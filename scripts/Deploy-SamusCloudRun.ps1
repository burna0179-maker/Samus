<#
.SYNOPSIS
  Build, push, and deploy the full Samus 21-workcell + 9-worker Cloud Run
  stack from the local Docker daemon.

.DESCRIPTION
  End-to-end automation of the CLOUD_RUN_DEPLOY.md runbook sections 4.1-4.3.
  Deploys callees before callers (tiered ordering), captures service URLs,
  and re-updates callers with discovered callee URLs. Also deploys the 9 SQS
  worker services (always-on, CPU-allocated, min-instances=1).

  Pipeline:
    1. Authenticate Docker to Artifact Registry
    2. Build + push samus-base (SHA-tagged + :latest)
    3. Build + push all 21 workcell images
    4. Deploy Tier 0 services (no inbound deps)
    5. Deploy Tier 1 services (call Tier 0)
    6. Deploy Tier 2 (gateway -- calls everything)
    7. Re-update Tier 1 services that need GATEWAY_URL
    8. Deploy Tier 3 (intake -- calls gateway)
    9. Deploy 9 SQS worker services

  Uses the OpenAI API key (not Anthropic) for LLM-consuming workcells.
  Requires the openai-api-key secret in GCP Secret Manager.

.PARAMETER Sha
  Image tag suffix. Defaults to current git HEAD short SHA.

.PARAMETER SkipBuild
  Skip image build steps. Use when images are already in the local daemon.

.PARAMETER SkipPush
  Skip image push steps. Use when images are already in Artifact Registry.

.PARAMETER SkipDeploy
  Skip deploy steps. Use to build+push only.

.PARAMETER SkipWorkers
  Skip deploying the 9 SQS worker services.

.PARAMETER OnlyServices
  Comma-separated subset of workcells to build/push/deploy.
  e.g. -OnlyServices gateway,memory,finance

.PARAMETER BuildContext
  Path to the Hustleforge repo root. Default: D:\Hustleforge
#>

[CmdletBinding()]
param(
    [string]$Sha,
    [switch]$SkipBuild,
    [switch]$SkipPush,
    [switch]$SkipDeploy,
    [switch]$SkipWorkers,
    [string[]]$OnlyServices,
    [string]$BuildContext = 'D:\Hustleforge'
)

# Native gcloud/docker writes WARNINGs to stderr which PS 5.1 wraps as
# NativeCommandError — under 'Stop' a benign warning aborts the whole deploy.
# Use 'Continue' so the script proceeds and surfaces real failures via the
# $LASTEXITCODE / $buildErrors / $deployErrors trackers it already maintains.
$ErrorActionPreference = 'Continue'

$Project   = '${GCP_PROJECT}'
$Region    = 'us-west1'
$ArRepo    = "us-west1-docker.pkg.dev/$Project/samus"
$RuntimeSa = 'samus-runtime@${GCP_PROJECT}.iam.gserviceaccount.com'

if (-not (Test-Path $BuildContext)) { throw "Build context not found: $BuildContext" }
Push-Location $BuildContext

if (-not $Sha) { $Sha = (git rev-parse --short HEAD 2>$null) }
if (-not $Sha) { $Sha = 'latest' }
$Sha = $Sha.Trim()

Write-Host ""
Write-Host "=== Samus Cloud Run Deploy ===" -ForegroundColor Cyan
Write-Host "  SHA        : $Sha"
Write-Host "  Context    : $BuildContext"
Write-Host "  AR repo    : $ArRepo"
Write-Host "  Project    : $Project"
Write-Host "  Runtime SA : $RuntimeSa"
Write-Host ""

# ======================================================================
# Common secret/env fragments
# ======================================================================
$Hmac    = "SAMUS_SHARED_HMAC_KEY=shared-hmac-key:latest"
$OpenAi  = "OPENAI_API_KEY=openai-api-key:latest"
$Aws     = "AWS_ACCESS_KEY_ID=aws-access-key-id:latest,AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest"
$AwsEnv  = "AWS_REGION=us-west-1,AWS_DEFAULT_REGION=us-west-1"
$BaseEnv = "PYTHONUNBUFFERED=1,SAMUS_ENV=production,$AwsEnv"
$LlmEnv  = "SAMUS_LLM_REINVEST_PCT=0.05,SAMUS_LLM_DAILY_FLOOR_USD=0.50,SAMUS_LLM_DAILY_CEILING_USD=25.00"
$LedgerEnv = "SAMUS_LEDGER_BACKEND=firestore,SAMUS_ARTIFACT_ROOT=gs://${GCP_PROJECT}-samus-data/artifacts"

# ======================================================================
# Service definitions -- tiered deployment order
# ======================================================================
# Dir->Image mapping (underscore dirs get hyphenated image names)
$ImageName = @{
    'gateway'='gateway'; 'leadgen'='leadgen'; 'prospecting'='prospecting';
    'scaffold'='scaffold'; 'fulfillment'='fulfillment'; 'memory'='memory';
    'ses'='feedback'; 'outreach'='outreach'; 'optimizer'='optimizer';
    'proposal'='proposal'; 'seo'='seo'; 'finance'='finance';
    'voice'='voice'; 'intake'='intake'; 'crm'='crm'; 'strategy'='strategy';
    'signal_filter'='signal-filter'; 'path_optimizer'='path-optimizer';
    'template_recovery'='template-recovery'; 'portfolio_controller'='portfolio-controller';
    'entropy'='entropy'
}

# Tier 0: no inbound dependencies
$Tier0 = @(
    # memory workcell deferred until AuraDB is provisioned (NEO4J_URI/USER secrets missing).
    @{ Name='crm';      Dir='crm';      Service='samus-crm';     Ingress='internal'; Auth=$false; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=crm,$BaseEnv,$LedgerEnv,DDB_PROSPECTS_TABLE=samus_prospects,DDB_CONTACTS_TABLE=samus_contacts,DDB_CONVERSATIONS_TABLE=samus_conversations,DDB_CALL_STATE_TABLE=samus_call-State,DDB_OPPORTUNITIES_TABLE=samus_opportunities,DDB_OPERATOR_TASKS_TABLE=samus_operator_tasks,DDB_ARTIFACTS_TABLE=samus_artifacts,DDB_ONBOARDING_LEADS_TABLE=samus_onboarding_leads";
       Secrets="$Hmac,$Aws" }
    @{ Name='finance';  Dir='finance';  Service='samus-finance';  Ingress='all';      Auth=$true;  Min=1; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=finance,$BaseEnv,$LedgerEnv,EMAIL_BACKEND=sendgrid";
       Secrets="$Hmac,STRIPE_API_KEY=stripe-api-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest,SENDGRID_API_KEY=sendgrid-api-key:latest,SENDGRID_FROM_EMAIL=sendgrid-from-email:latest,$Aws" }
    @{ Name='leadgen';     Dir='leadgen';              Service='samus-leadgen';              Ingress='internal'; Auth=$false; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=leadgen,$BaseEnv";     Secrets="$Hmac,$Aws" }
    @{ Name='outreach';    Dir='outreach';             Service='samus-outreach';             Ingress='internal'; Auth=$false; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=outreach,$BaseEnv,$LlmEnv";    Secrets="$Hmac,$OpenAi,$Aws" }
    @{ Name='optimizer';   Dir='optimizer';            Service='samus-optimizer';            Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=optimizer,$BaseEnv";   Secrets="$Hmac,$Aws" }
    @{ Name='signal_filter';        Dir='signal_filter';        Service='samus-signal-filter';        Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=signal_filter,$BaseEnv";        Secrets="$Hmac,$Aws" }
    @{ Name='path_optimizer';       Dir='path_optimizer';       Service='samus-path-optimizer';       Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=path_optimizer,$BaseEnv";       Secrets="$Hmac,$Aws" }
    @{ Name='template_recovery';    Dir='template_recovery';    Service='samus-template-recovery';    Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=template_recovery,$BaseEnv";    Secrets="$Hmac,$Aws" }
    @{ Name='portfolio_controller'; Dir='portfolio_controller'; Service='samus-portfolio-controller'; Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=portfolio_controller,$BaseEnv"; Secrets="$Hmac,$Aws" }
    @{ Name='entropy';     Dir='entropy';              Service='samus-entropy';              Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=entropy,$BaseEnv";     Secrets="$Hmac,$Aws" }
)

# Tier 1: call Tier 0 services
$Tier1 = @(
    @{ Name='prospecting'; Dir='prospecting'; Service='samus-prospecting'; Ingress='internal'; Auth=$false; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=prospecting,$BaseEnv,$LlmEnv";
       Secrets="$Hmac,$OpenAi,GOOGLE_PLACES_API_KEY=places-api-key:latest,$Aws";
       NeedsGatewayUrl=$false }
    @{ Name='scaffold';    Dir='scaffold';    Service='samus-scaffold';    Ingress='internal'; Auth=$false; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=scaffold,$BaseEnv";
       Secrets="$Hmac,$Aws"; NeedsGatewayUrl=$false }
    @{ Name='fulfillment'; Dir='fulfillment'; Service='samus-fulfillment'; Ingress='internal'; Auth=$false; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=fulfillment,$BaseEnv";
       Secrets="$Hmac,$Aws"; NeedsGatewayUrl=$false }
    @{ Name='seo';         Dir='seo';         Service='samus-seo';         Ingress='internal'; Auth=$false; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=seo,$BaseEnv,$LlmEnv";
       Secrets="$Hmac,$OpenAi,$Aws";
       NeedsGatewayUrl=$true }
    @{ Name='proposal';    Dir='proposal';    Service='samus-proposal';    Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=proposal,$BaseEnv";
       Secrets="$Hmac,$Aws"; NeedsGatewayUrl=$true }
    @{ Name='strategy';    Dir='strategy';    Service='samus-strategy';    Ingress='internal'; Auth=$false; Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=strategy,$BaseEnv,$LlmEnv";
       Secrets="$Hmac,$OpenAi,$Aws"; NeedsGatewayUrl=$true }
    @{ Name='voice';       Dir='voice';       Service='samus-voice';       Ingress='all';      Auth=$true;  Min=1; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=voice,$BaseEnv,$LlmEnv";
       Secrets="$Hmac,$OpenAi,VAPI_API_KEY=vapi-api-key:latest,VAPI_WEBHOOK_SECRET=vapi-webhook-secret:latest,$Aws";
       NeedsGatewayUrl=$false }
    @{ Name='feedback';    Dir='ses';         Service='samus-feedback';    Ingress='all';      Auth=$true;  Min=0; Max=2; Mem='512Mi';
       Env="SAMUS_SERVICE=feedback,$BaseEnv";
       Secrets="$Hmac,$Aws"; NeedsGatewayUrl=$false }
)

# Tier 2: gateway (calls everything)
$Tier2 = @(
    @{ Name='gateway'; Dir='gateway'; Service='samus-gateway'; Ingress='all'; Auth=$true; Min=1; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=gateway,$BaseEnv,$LedgerEnv,$LlmEnv,SAMUS_AUTHZ_MODE=audit";
       Secrets="$Hmac,$OpenAi,$Aws" }
)

# Tier 3: intake (calls gateway)
$Tier3 = @(
    @{ Name='intake'; Dir='intake'; Service='samus-intake'; Ingress='all'; Auth=$true; Min=0; Max=4; Mem='512Mi';
       Env="SAMUS_SERVICE=intake,$BaseEnv,$LlmEnv,DDB_ONBOARDING_LEADS_TABLE=samus_onboarding_leads,SAMUS_INTAKE_ALLOWED_ORIGINS=https://hustleforge.tech,SAMUS_INTAKE_RATE_LIMIT_ENABLED=1,SAMUS_TELEMETRY_INGEST_ENABLED=true,SAMUS_INTAKE_TELEMETRY_PATH=/opt/samus/data/intake/site_telemetry.jsonl,OUTREACH_WEBSITE_FORM_WARM_ENROLL_ENABLED=true";
       Secrets="$Hmac,$OpenAi,$Aws" }
)

# Workers: one per SQS-bearing workcell
$WorkerSvcs = @('leadgen','prospecting','scaffold','fulfillment','feedback','outreach','optimizer','proposal','seo')
$LlmWorkers = @('prospecting','outreach','seo')

# All services for build/push
$AllServices = $Tier0 + $Tier1 + $Tier2 + $Tier3

if ($OnlyServices) {
    $AllServices = $AllServices | Where-Object { $OnlyServices -contains $_.Name }
    Write-Host "Filtered to $($AllServices.Count) service(s): $($AllServices.Name -join ', ')" -ForegroundColor Yellow
}

# ======================================================================
# Helpers
# ======================================================================
function Get-CRUrl([string]$svc) {
    $url = gcloud run services describe $svc --region=$Region --project=$Project --format='value(status.url)' 2>$null
    if ($LASTEXITCODE -eq 0 -and $url) { return $url.Trim() }
    return $null
}

function Deploy-Service($svc) {
    $dir = if ($svc.Dir) { $svc.Dir } else { $svc.Name }
    $imgName = $ImageName[$dir]
    if (-not $imgName) { $imgName = $svc.Name -replace '_','-' }
    $img = "$ArRepo/samus-$($imgName):$Sha"

    $deployArgs = @(
        'run', 'deploy', $svc.Service,
        "--image=$img",
        "--region=$Region",
        '--platform=managed',
        '--execution-environment=gen2',
        "--ingress=$($svc.Ingress)",
        '--port=8080',
        "--memory=$($svc.Mem)",
        '--cpu=1',
        "--service-account=$RuntimeSa",
        "--set-env-vars=$($svc.Env)",
        "--set-secrets=$($svc.Secrets)",
        "--project=$Project",
        "--min-instances=$($svc.Min)",
        "--max-instances=$($svc.Max)",
        '--quiet'
    )
    if ($svc.Auth) {
        $deployArgs += '--allow-unauthenticated'
    } else {
        $deployArgs += '--no-allow-unauthenticated'
    }

    Write-Host "  Deploying $($svc.Service) ($($svc.Ingress), min=$($svc.Min))..." -ForegroundColor Cyan
    gcloud @deployArgs 2>&1 | Out-Host
    return $LASTEXITCODE
}

# ======================================================================
# 1. Docker auth
# ======================================================================
Write-Host "[1] Configuring Docker auth for AR..." -ForegroundColor Cyan
gcloud auth configure-docker us-west1-docker.pkg.dev --quiet 2>&1 | Out-Null

# ======================================================================
# 2-3. Build + push base
# ======================================================================
$BaseSha    = "$ArRepo/samus-base:$Sha"
$BaseLatest = "$ArRepo/samus-base:latest"

if (-not $SkipBuild) {
    Write-Host "[2] Building samus-base ($Sha)..." -ForegroundColor Cyan
    docker build -f Samus/docker/base/Dockerfile -t $BaseSha -t $BaseLatest . 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "base build failed" }
}

if (-not $SkipPush) {
    Write-Host "[3] Pushing samus-base..." -ForegroundColor Cyan
    docker push $BaseSha    2>&1 | Out-Host; if ($LASTEXITCODE -ne 0) { throw "base push (sha) failed" }
    docker push $BaseLatest 2>&1 | Out-Host; if ($LASTEXITCODE -ne 0) { throw "base push (latest) failed" }
}

# ======================================================================
# 4. Build + push all workcell images
# ======================================================================
$buildErrors = @()
$pushErrors  = @()

foreach ($svc in $AllServices) {
    $dir = if ($svc.Dir) { $svc.Dir } else { $svc.Name }
    $imgName = $ImageName[$dir]
    if (-not $imgName) { $imgName = $svc.Name -replace '_','-' }

    $imgSha    = "$ArRepo/samus-$($imgName):$Sha"
    $imgLatest = "$ArRepo/samus-$($imgName):latest"
    $dockerfile = "Samus/docker/workcells/$dir/Dockerfile"

    if (-not $SkipBuild) {
        Write-Host "[4] Building samus-$imgName..." -ForegroundColor Cyan
        docker build --build-arg "BASE_IMAGE=$BaseSha" -f $dockerfile -t $imgSha -t $imgLatest . 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { $buildErrors += $svc.Name; continue }
    }

    if (-not $SkipPush) {
        Write-Host "[5] Pushing samus-$imgName..." -ForegroundColor Cyan
        docker push $imgSha    2>&1 | Out-Host; if ($LASTEXITCODE -ne 0) { $pushErrors += "$($svc.Name):sha";    continue }
        docker push $imgLatest 2>&1 | Out-Host; if ($LASTEXITCODE -ne 0) { $pushErrors += "$($svc.Name):latest"; continue }
    }
}

if ($buildErrors) { Write-Host "BUILD ERRORS: $($buildErrors -join ', ')" -ForegroundColor Red }
if ($pushErrors)  { Write-Host "PUSH ERRORS:  $($pushErrors  -join ', ')" -ForegroundColor Red }

# ======================================================================
# 6-9. Tiered deploy
# ======================================================================
$deployErrors  = @()
$discoveredUrls = @{}

if (-not $SkipDeploy) {
    # --- Tier 0 ---
    Write-Host ""
    Write-Host "=== TIER 0 (no inbound deps) ===" -ForegroundColor Yellow
    foreach ($svc in $Tier0) {
        if ($buildErrors -contains $svc.Name -or ($pushErrors -match $svc.Name)) {
            Write-Host "  SKIP $($svc.Service) (upstream failure)" -ForegroundColor DarkYellow
            continue
        }
        if ($OnlyServices -and ($OnlyServices -notcontains $svc.Name)) { continue }
        $rc = Deploy-Service $svc
        if ($rc -ne 0) { $deployErrors += $svc.Service; continue }
        $url = Get-CRUrl $svc.Service
        if ($url) { $discoveredUrls[$svc.Name] = $url }
    }

    # --- Tier 1 ---
    Write-Host ""
    Write-Host "=== TIER 1 (call Tier 0) ===" -ForegroundColor Yellow
    foreach ($svc in $Tier1) {
        if ($buildErrors -contains $svc.Name -or ($pushErrors -match $svc.Name)) {
            Write-Host "  SKIP $($svc.Service) (upstream failure)" -ForegroundColor DarkYellow
            continue
        }
        if ($OnlyServices -and ($OnlyServices -notcontains $svc.Name)) { continue }
        if ($discoveredUrls['memory']) {
            $svc.Env += ",MEMORY_URL=$($discoveredUrls['memory'])"
        }
        $rc = Deploy-Service $svc
        if ($rc -ne 0) { $deployErrors += $svc.Service; continue }
        $url = Get-CRUrl $svc.Service
        if ($url) { $discoveredUrls[$svc.Name] = $url }
    }

    # --- Tier 2: gateway ---
    Write-Host ""
    Write-Host "=== TIER 2 (gateway) ===" -ForegroundColor Yellow
    foreach ($svc in $Tier2) {
        if ($OnlyServices -and ($OnlyServices -notcontains $svc.Name)) { continue }
        $urlEnvParts = @()
        foreach ($depName in @('leadgen','prospecting','scaffold','fulfillment','memory','crm','finance',
                               'signal_filter','path_optimizer','template_recovery','portfolio_controller',
                               'entropy','outreach','optimizer','proposal','seo','strategy','feedback','voice')) {
            $envKey = ($depName -replace '_','').ToUpper() + '_URL'
            if ($discoveredUrls[$depName]) { $urlEnvParts += "$envKey=$($discoveredUrls[$depName])" }
        }
        if ($urlEnvParts) { $svc.Env += "," + ($urlEnvParts -join ',') }
        $rc = Deploy-Service $svc
        if ($rc -ne 0) { $deployErrors += $svc.Service; continue }
        $url = Get-CRUrl $svc.Service
        if ($url) { $discoveredUrls['gateway'] = $url }
    }

    # --- Tier 2 fix-up: re-update services that need GATEWAY_URL ---
    if ($discoveredUrls['gateway']) {
        Write-Host ""
        Write-Host "=== TIER 2 fix-up (inject GATEWAY_URL) ===" -ForegroundColor Yellow
        $gwUrl = $discoveredUrls['gateway']
        foreach ($svc in $Tier1) {
            if (-not $svc.NeedsGatewayUrl) { continue }
            if ($OnlyServices -and ($OnlyServices -notcontains $svc.Name)) { continue }
            Write-Host "  Updating $($svc.Service) with GATEWAY_URL..." -ForegroundColor Cyan
            gcloud run services update $svc.Service --region=$Region --project=$Project `
                --update-env-vars="GATEWAY_URL=$gwUrl,SAMUS_GATEWAY_URL=$gwUrl" --quiet 2>&1 | Out-Host
        }
        gcloud run services update samus-finance --region=$Region --project=$Project `
            --update-env-vars="GATEWAY_URL=$gwUrl" --quiet 2>&1 | Out-Host
    }

    # --- Tier 3: intake ---
    Write-Host ""
    Write-Host "=== TIER 3 (intake) ===" -ForegroundColor Yellow
    foreach ($svc in $Tier3) {
        if ($OnlyServices -and ($OnlyServices -notcontains $svc.Name)) { continue }
        $rc = Deploy-Service $svc
        if ($rc -ne 0) { $deployErrors += $svc.Service }
    }

    # --- Workers ---
    if (-not $SkipWorkers) {
        Write-Host ""
        Write-Host "=== WORKERS (always-on SQS pollers) ===" -ForegroundColor Yellow
        foreach ($wName in $WorkerSvcs) {
            if ($OnlyServices -and ($OnlyServices -notcontains $wName)) { continue }
            $dir = $wName
            if ($wName -eq 'feedback') { $dir = 'ses' }
            $imgName = $ImageName[$dir]
            if (-not $imgName) { $imgName = $wName -replace '_','-' }
            $workerService = "samus-$imgName-worker"
            $img = "$ArRepo/samus-$($imgName):$Sha"

            $wSecrets = "$Hmac,$Aws"
            $wEnvVars = "SAMUS_SERVICE=$wName,$BaseEnv"
            if ($LlmWorkers -contains $wName) {
                $wSecrets = "$Hmac,$OpenAi,$Aws"
                $wEnvVars = "SAMUS_SERVICE=$wName,$BaseEnv,$LlmEnv"
            }

            Write-Host "  Deploying $workerService..." -ForegroundColor Cyan
            gcloud run deploy $workerService `
                --image=$img `
                --region=$Region `
                --platform=managed `
                --execution-environment=gen2 `
                --ingress=internal `
                --no-allow-unauthenticated `
                --min-instances=1 --max-instances=1 `
                --no-cpu-throttling `
                --cpu=1 --memory=512Mi `
                --concurrency=1 `
                --service-account=$RuntimeSa `
                --set-env-vars="$wEnvVars" `
                --set-secrets="$wSecrets" `
                --command=python --args="-m,backend.$wName.worker" `
                --project=$Project `
                --quiet 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { $deployErrors += $workerService }
        }
    }
}

# ======================================================================
# Summary
# ======================================================================
Write-Host ""
Write-Host "=== Deploy Summary ===" -ForegroundColor Cyan
Write-Host "  Services built : $($AllServices.Count)"
Write-Host "  Workers        : $(if ($SkipWorkers) { 'skipped' } else { $WorkerSvcs.Count })"
Write-Host "  Build errors   : $($buildErrors.Count)"
Write-Host "  Push errors    : $($pushErrors.Count)"
Write-Host "  Deploy errors  : $($deployErrors.Count)"
if ($buildErrors)  { Write-Host "    builds:  $($buildErrors  -join ', ')" -ForegroundColor Red }
if ($pushErrors)   { Write-Host "    pushes:  $($pushErrors   -join ', ')" -ForegroundColor Red }
if ($deployErrors) { Write-Host "    deploys: $($deployErrors -join ', ')" -ForegroundColor Red }

Write-Host ""
Write-Host "Discovered URLs:" -ForegroundColor Cyan
foreach ($k in ($discoveredUrls.Keys | Sort-Object)) {
    Write-Host "  $($k.PadRight(25)) $($discoveredUrls[$k])"
}

if ($deployErrors.Count -eq 0 -and $buildErrors.Count -eq 0) {
    Write-Host ""
    Write-Host "All services deployed successfully." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Verify:   gcloud run services list --region=$Region --project=$Project"
    Write-Host "  2. Health:   curl -fsS `"$($discoveredUrls['gateway'])/health`""
    Write-Host "  3. Schedule: .\Register-CloudSchedulerJobs.ps1"
} else {
    Write-Host ""
    Write-Host "Some services failed -- check errors above." -ForegroundColor Red
}

Pop-Location
