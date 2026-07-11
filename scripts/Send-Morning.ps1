<#
.SYNOPSIS
  Send the Samus morning briefing to email + Discord (uses the same renderer
  as Show-Morning.ps1, just pushes the output to configured channels).

.DESCRIPTION
  Wraps ``python -m backend.morning_send`` with the secret-handling pattern
  used elsewhere in the Samus scripts. Pulls credentials from DPAPI
  (Scope=Samus) and sets them only on the child Python process's env;
  every env mutation is reversed in the finally block so this script does
  not leak state into the caller's shell.

  Secrets read (each is optional -- missing one disables that channel):

    StripeApiKey               -> STRIPE_API_KEY               (briefing's live MRR pull)
    SendgridApiKey             -> SENDGRID_API_KEY             (email channel)
    SendgridFromEmail          -> SENDGRID_FROM_EMAIL          (verified sender)
    DiscordBriefWebhookUrl     -> SAMUS_BRIEF_DISCORD_WEBHOOK  (discord channel)

  Email recipient defaults to env `SAMUS_MORNING_EMAIL_TO`; override with -EmailTo.
  Pass -NoEmail / -NoDiscord to skip a channel without removing its secret.

  Exit code: 0 if at least one configured channel succeeded (or both were
  intentionally skipped); non-zero if any configured channel failed.

.PARAMETER EmailTo
  Override the email recipient. Default: env SAMUS_MORNING_EMAIL_TO.

.PARAMETER NoEmail
  Skip the email channel even if the SendGrid secrets are populated.

.PARAMETER NoDiscord
  Skip the Discord channel even if the webhook secret is populated.

.PARAMETER OpenConsole
  After the brief is pushed, open the operator voice console in the default
  browser (delegates to Open-VoiceConsole.ps1 -StartStackIfDown, so the Samus
  stack is brought up first if the voice workcell isn't already running). The
  registered morning-brief task passes this so the console is part of the
  operator's 08:00 routine; a plain manual run does NOT open a browser unless
  this switch is given.

.EXAMPLE
  .\Send-Morning.ps1

.EXAMPLE
  # Send only to Discord (e.g. while SendGrid sender is being re-verified)
  .\Send-Morning.ps1 -NoEmail

.EXAMPLE
  # One-time setup -- store the secrets before the first run
  Import-Module D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1
  Set-HfSecret -Scope Samus -Name SendgridApiKey         # paste SG.* token
  Set-HfSecret -Scope Samus -Name SendgridFromEmail      # verified sender, e.g. brief@hustleforge.tech
  Set-HfSecret -Scope Samus -Name DiscordBriefWebhookUrl # https://discord.com/api/webhooks/<id>/<token>
#>

[CmdletBinding()]
param(
    [Parameter()][string]$EmailTo$EmailTo = $env:SAMUS_MORNING_EMAIL_TO,
    [switch]$NoEmail,
    [switch]$NoDiscord,
    [switch]$OpenConsole,
    # ADR-018 pre-shift attestation: POST /api/gateway/pre_shift_briefing BEFORE
    # rendering the brief so today's autonomous B2B live dial gate opens. Default
    # ON: the operator's decision to schedule this morning task IS the daily
    # acknowledgement. Pass -NoAutoAttest to revert to fully manual (brief still
    # renders with a red NOT ATTESTED banner + the manual command). Kill switch:
    # disable the scheduled task = fully manual regardless of this flag.
    [switch]$NoAutoAttest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$samusRoot  = Resolve-Path (Join-Path $here '..')
$venvPython = Join-Path $samusRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Run from Samus repo root with .venv installed."
}

$sharedModule = 'D:\Hustleforge\_shared\scripts\Hustleforge.Secrets.psm1'
if (-not (Test-Path $sharedModule)) {
    throw "Required secrets module not found at $sharedModule."
}
Import-Module $sharedModule -Force

# --- transcript capture ------------------------------------------------------
# Scheduled task runs powershell.exe with -WindowStyle Hidden, which means no
# console is captured anywhere -- failures leave NO evidence. Start-Transcript
# writes everything (Write-Host, Warning, native stdout/stderr from python) to
# a per-run file under logs/morning_brief/.
$logDir = Join-Path $samusRoot 'logs\morning_brief'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir ("morning_brief_$((Get-Date).ToString('yyyy-MM-dd_HHmmss')).log")
$transcriptStarted = $false
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Host "transcript -> $logPath"
} catch {
    Write-Warning "Start-Transcript failed (continuing without log): $_"
}

# Bounded rotation: keep last 30 logs so years of daily fires don't accumulate
# under the artifact root. Tiny files, but unbounded growth is still growth.
try {
    Get-ChildItem $logDir -Filter 'morning_brief_*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { Write-Warning "log rotation failed: $_" }

# Default exit code so the outer 'exit $exit' has something to return even if
# an exception fires before the python invocation sets it.
$exit = 1

try {

# --- env helpers (push + restore in finally) ---------------------------------

$envBackup = @{}

function Push-EnvVar([string]$Name, [string]$Value) {
    if (-not $envBackup.ContainsKey($Name)) {
        $envBackup[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    Set-Item -Path "Env:$Name" -Value $Value
}

function Restore-EnvVars {
    foreach ($k in $envBackup.Keys) {
        $v = $envBackup[$k]
        if ($null -eq $v) {
            Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}

# --- load secrets ------------------------------------------------------------

# Stripe key is optional -- briefing's CASH section degrades to
# "stripe_api_key_unset" inline; the rest of the briefing still renders.
try {
    if (Test-HfSecret -Scope Samus -Name StripeApiKey) {
        Push-EnvVar 'STRIPE_API_KEY' (Get-HfSecret -Scope Samus -Name StripeApiKey)
    }
} catch {
    Write-Warning "Stripe key load failed -- briefing CASH section will skip live Stripe: $_"
}

# Email channel -- both secrets required; if either is missing, the python
# sender raises ValueError and the email channel reports FAIL.
if (-not $NoEmail) {
    $emailReady = $true
    if (Test-HfSecret -Scope Samus -Name SendgridApiKey) {
        Push-EnvVar 'SENDGRID_API_KEY' (Get-HfSecret -Scope Samus -Name SendgridApiKey)
    } else {
        Write-Warning "SendgridApiKey not in DPAPI -- email channel will fail. Run: Set-HfSecret -Scope Samus -Name SendgridApiKey"
        $emailReady = $false
    }
    if (Test-HfSecret -Scope Samus -Name SendgridFromEmail) {
        Push-EnvVar 'SENDGRID_FROM_EMAIL' (Get-HfSecret -Scope Samus -Name SendgridFromEmail)
    } elseif (-not $env:SENDGRID_FROM_EMAIL) {
        Write-Warning "SendgridFromEmail not in DPAPI or env -- email channel will fail. Run: Set-HfSecret -Scope Samus -Name SendgridFromEmail"
        $emailReady = $false
    }
    # Always set the recipient when email channel is enabled -- the python
    # sender treats SAMUS_BRIEF_EMAIL_TO as the channel-enable flag.
    if ($emailReady) {
        Push-EnvVar 'SAMUS_BRIEF_EMAIL_TO' $EmailTo
    }
}

# Discord channel
if (-not $NoDiscord) {
    if (Test-HfSecret -Scope Samus -Name DiscordBriefWebhookUrl) {
        Push-EnvVar 'SAMUS_BRIEF_DISCORD_WEBHOOK' (Get-HfSecret -Scope Samus -Name DiscordBriefWebhookUrl)
    } else {
        Write-Warning "DiscordBriefWebhookUrl not in DPAPI -- discord channel will skip. Run: Set-HfSecret -Scope Samus -Name DiscordBriefWebhookUrl"
    }
}

# --- CRM / pipeline / graph data plane (SEAM 2 host fix, ported from 5176c2d) -
# The brief's SALES section reads CRM CallState (follow-ups) + the stale-deal
# sweep from DynamoDB, and the memory graph from Neo4j. Under docker those env
# vars are injected by Start-SamusStack.ps1; a host scheduled-task run gets
# NONE of them, so the brief logs "crm scan failed: Unable to locate
# credentials", "neo4j_unavailable", and runs SAMUS_ENV-unset (dev mode). This
# block mirrors the stack's wiring so the host brief sees the same data plane
# the containers do. Every var rides Push-EnvVar so it is reversed in finally.
# Best-effort with Test-HfSecret guards + a clear Write-Warning when absent.

# AWS creds for the CRM/DDB lanes. DPAPI AwsAccessKeyId/AwsSecretAccessKey ->
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (the exact mapping Start-SamusStack
# applies). Region pinned to us-west-1 to match the stack. Absent creds keep
# today's degrade-gracefully posture: SALES lanes are simply omitted.
if ((Test-HfSecret -Scope Samus -Name AwsAccessKeyId) -and
    (Test-HfSecret -Scope Samus -Name AwsSecretAccessKey)) {
    Push-EnvVar 'AWS_ACCESS_KEY_ID'     (Get-HfSecret -Scope Samus -Name AwsAccessKeyId)
    Push-EnvVar 'AWS_SECRET_ACCESS_KEY' (Get-HfSecret -Scope Samus -Name AwsSecretAccessKey)
    if (-not $env:AWS_REGION)         { Push-EnvVar 'AWS_REGION'         'us-west-1' }
    if (-not $env:AWS_DEFAULT_REGION) { Push-EnvVar 'AWS_DEFAULT_REGION' 'us-west-1' }
} else {
    Write-Warning "AWS creds (AwsAccessKeyId/AwsSecretAccessKey) not in DPAPI -- the SALES follow-ups + stale-deal sweep lanes will be omitted. Run: Set-HfSecret -Scope Samus -Name AwsAccessKeyId  (and AwsSecretAccessKey)"
}

# Match the running stack's runtime env so the brief is NOT silently in dev
# mode. The Samus stack is in production (SAMUS_ENV=production in
# docker/compose/.env since 2026-06-22). Pin 'production' here so the host
# brief matches: many security fail-closed gates fire only in production, and
# we want the brief's reads to honour them. Operator override via $env:SAMUS_ENV
# is respected.
if (-not $env:SAMUS_ENV) {
    Push-EnvVar 'SAMUS_ENV' 'production'
}

# Neo4j wiring for the memory-graph reads. DPAPI HivemindPassword ->
# NEO4J_PASSWORD; NEO4J_USER=neo4j_samus is the per-agent RBAC identity. The
# brief degrades gracefully when the graph is down (neo4j_required=false), so
# a missing password only WARNs.
if (-not $env:NEO4J_URI)      { Push-EnvVar 'NEO4J_URI' 'bolt://localhost:7687' }
if (-not $env:NEO4J_USER)     { Push-EnvVar 'NEO4J_USER' 'neo4j_samus' }
if (-not $env:NEO4J_REQUIRED) { Push-EnvVar 'NEO4J_REQUIRED' 'false' }
if (Test-HfSecret -Scope Samus -Name HivemindPassword) {
    Push-EnvVar 'NEO4J_PASSWORD' (Get-HfSecret -Scope Samus -Name HivemindPassword)
} else {
    Write-Warning "HivemindPassword not in DPAPI -- the memory graph stays unavailable (brief degrades: NEO4J_REQUIRED=false). Run: Set-HfSecret -Scope Samus -Name HivemindPassword"
}

# UTF-8 + no-color (defensive -- child sets SAMUS_MORNING_NO_COLOR anyway).
Push-EnvVar 'PYTHONIOENCODING' 'utf-8'
Push-EnvVar 'SAMUS_MORNING_NO_COLOR' '1'

# Pin SAMUS_ARTIFACT_ROOT to the Samus runtime's own .data\host_artifacts
# ($samusRoot-relative), in lockstep with Run-ProspectingDaily.ps1 so producer
# and consumer share ONE path that follows the checkout. (Was hardcoded to a
# standalone D:\Hustleforge\Samus copy that diverged from the worktree and reset
# the prospecting ring.) The storage.root() default E:\Hustleforge\Samus\data\
# artifacts has Phase-1 ACL Alex:(N). Override via $env:SAMUS_ARTIFACT_ROOT.
if (-not $env:SAMUS_ARTIFACT_ROOT) {
    Push-EnvVar 'SAMUS_ARTIFACT_ROOT' (Join-Path $samusRoot '.data\host_artifacts')
}

# --- ADR-018 auto pre-shift attestation (Option A default) -------------------
# POST /api/gateway/pre_shift_briefing so today's autonomous B2B live dial gate
# opens BEFORE the brief renders. On success:
#   - attestation stamped in artifacts/cognition/ (bind-mounted to host)
#   - the brief's AUTONOMOUS DAY GATE section shows green ATTESTED
#   - governed dial fires within call-hours through the rest of the day
# On failure (gateway down, LLM error, network fault), the brief STILL renders
# and shows the red NOT ATTESTED banner + the manual command; a fault here must
# never block the morning push. Skip via -NoAutoAttest for fully manual.
if (-not $NoAutoAttest) {
    $attestUrl = 'http://127.0.0.1:8100/api/gateway/pre_shift_briefing'
    $prevAttestEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $resp = Invoke-RestMethod -Method Post -Uri $attestUrl -TimeoutSec 90
        if ($resp -and $resp.PSObject.Properties.Match('briefing_id').Count -gt 0) {
            Write-Host "ADR-018 attestation POSTed: briefing_id=$($resp.briefing_id) ingested=$($resp.ingested)"
        } else {
            Write-Warning "ADR-018 attestation POST returned unexpected shape (brief will still render)"
        }
    } catch {
        Write-Warning "ADR-018 attestation POST failed ($($_.Exception.Message)) -- brief will show NOT ATTESTED banner"
    } finally {
        $ErrorActionPreference = $prevAttestEAP
    }
} else {
    Write-Host "ADR-018 attestation: SKIPPED (-NoAutoAttest). Brief will render manual-attest banner if not already stamped."
}

# --- ADR-014: sync the in-stack call list out of the container ---------------
# The daily prospecting producer runs in-stack (ADR-014, forward-ported to
# samus 2026-06-23): the samus-prospecting cadence writes call_list_*.csv /
# morning_call_list_*.txt to the samus-data volume (container
# SAMUS_ARTIFACT_ROOT=/opt/samus/data/artifacts), NOT to the host
# host_artifacts root this brief reads. Under Docker Desktop/WSL2 that named
# volume is not a Windows path, so the host cannot read it directly. Pull
# today's two artifacts into the host daily_calls dir before the brief
# renders. Fail-soft: any docker error leaves the brief to degrade to "no
# call list today" exactly as before -- it must never block the morning push.
# (Native-exe stderr is wrapped as an ErrorRecord under EAP=Stop, so localize
# EAP to Continue around the calls.)
$prevSyncEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $today        = (Get-Date).ToString('yyyy-MM-dd')
    $hostCallsDir = Join-Path $env:SAMUS_ARTIFACT_ROOT 'daily_calls'
    if (-not (Test-Path $hostCallsDir)) {
        New-Item -ItemType Directory -Path $hostCallsDir -Force | Out-Null
    }
    $containerRoot = (& docker exec samus-prospecting printenv SAMUS_ARTIFACT_ROOT 2>$null | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($containerRoot)) { $containerRoot = '/opt/samus/data/artifacts' }
    $containerRoot = $containerRoot.Trim()
    foreach ($name in @("call_list_$today.csv", "morning_call_list_$today.txt")) {
        & docker cp "samus-prospecting:$containerRoot/daily_calls/$name" (Join-Path $hostCallsDir $name) 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "ADR-014 sync: pulled $name from samus-prospecting"
        } else {
            Write-Warning "ADR-014 sync: $name not in container yet (brief may show no list)"
        }
    }
} catch {
    Write-Warning "ADR-014 call-list sync skipped (docker unavailable?): $($_.Exception.Message)"
} finally {
    $ErrorActionPreference = $prevSyncEAP
}

# --- invoke python sender ----------------------------------------------------

$prevConsoleEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# PS 5.1's Start-Transcript does not reliably capture native-exe stderr, so
# redirect python's stderr to a sidecar file and fold it into the transcript
# after the call. CRITICAL: `2>file` on a native exe still wraps each stderr
# line as a NativeCommandError ErrorRecord; with $ErrorActionPreference='Stop'
# (set at top of script) that throws immediately even when python exits 0.
# Localize EAP to 'Continue' around the invocation so the redirect is safe.
$stderrPath = "$logPath.err"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
Push-Location $samusRoot
try {
    & $venvPython -m backend.morning_send 2>$stderrPath
    $exit = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prevEAP
    Restore-EnvVars
    [Console]::OutputEncoding = $prevConsoleEnc
    if (Test-Path $stderrPath) {
        $stderrText = Get-Content $stderrPath -Raw -ErrorAction SilentlyContinue
        if ($stderrText) {
            Write-Host "--- python stderr ---"
            Write-Host $stderrText
            Write-Host "--- end stderr ---"
        }
        Remove-Item $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

# --- operator voice console --------------------------------------------------
# -OpenConsole (passed by the registered morning-brief task) rolls the operator
# voice console into the morning routine: once the brief is pushed, ensure the
# voice workcell is up (-StartStackIfDown brings the Samus stack up if it
# isn't) and open the browser console so a ready call surface is part of the
# operator's 08:00 start. Pure best-effort -- a failure here never changes the
# brief's exit code, and a plain `.\Send-Morning.ps1` (no switch) never pops a
# browser or starts the stack.
if ($OpenConsole) {
    $openConsoleScript = Join-Path $here 'Open-VoiceConsole.ps1'
    if (Test-Path $openConsoleScript) {
        try {
            & $openConsoleScript -StartStackIfDown
        } catch {
            Write-Warning "Open-VoiceConsole.ps1 failed (brief still sent): $_"
        }
    } else {
        Write-Warning "Open-VoiceConsole.ps1 not found at $openConsoleScript -- console not opened."
    }
}

} finally {
    # Grep-friendly EXIT marker so a log can be triaged in one line. Always
    # written, even if the python invocation never ran (e.g. throw from a
    # secret read above).
    Write-Host "EXIT: $exit"
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

exit $exit
