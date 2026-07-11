<#
.SYNOPSIS
  Register the two morning-campaign auto-fire tasks (email + dial) for Samus.

.DESCRIPTION
  Idempotent. Creates/refreshes weekday (Mon-Fri) tasks that run AFTER the
  07:30 prospecting + 08:00 brief so the day's fresh call list exists:
    'Samus Morning Outreach'  09:00  -> Run-MorningOutreach.ps1 -Live -MaxSend N
    'Samus Morning Dial'      09:10  -> Start-MorningDial.ps1 -Live -MaxCalls N
  Both are LIVE (they send/dial). Compliance gates stay on inside the scripts
  (CAN-SPAM/suppression for email; TCPA-hours/DNC/7d-cooldown for calls). Runs
  as the current interactive user so DPAPI (Scope=Samus) secrets decrypt.

.PARAMETER MaxSend / MaxCalls
  Per-run caps. Defaults: 10 emails, 15 calls. Tunable (intended to become
  CODB-governed once the finance reasoner is armed).
#>
[CmdletBinding()]
param(
    [Parameter()][int]$MaxSend   = 10,
    [Parameter()][int]$MaxCalls  = 15,
    [Parameter()][string]$OutreachTime = '09:00',
    [Parameter()][string]$DialTime     = '09:10'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$outreach = Join-Path $here 'Run-MorningOutreach.ps1'
$dial     = Join-Path $here 'Start-MorningDial.ps1'
foreach ($s in @($outreach, $dial)) { if (-not (Test-Path $s)) { throw "missing script: $s" } }

$me        = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$days      = @('Monday','Tuesday','Wednesday','Thursday','Friday')

$tasks = @(
    @{ Name='Samus Morning Outreach'; Time=$OutreachTime; Script=$outreach;
       Arg="-Live -MaxSend $MaxSend"; Limit=15 },
    @{ Name='Samus Morning Dial';     Time=$DialTime;     Script=$dial;
       Arg="-Live -MaxCalls $MaxCalls"; Limit=30 }
)

foreach ($t in $tasks) {
    if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes $t.Limit) `
                    -MultipleInstances IgnoreNew -Compatibility Win8
    $cmd = "& '$($t.Script)' $($t.Arg); exit `$LASTEXITCODE"
    $pwshArgs = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
    $action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $pwshArgs -WorkingDirectory $here
    $trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $t.Time
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "Samus morning campaign auto-fire (LIVE): $($t.Arg). Compliance gates enforced in-script." | Out-Null
    $ni = (Get-ScheduledTaskInfo -TaskName $t.Name).NextRunTime
    Write-Host ("Registered '{0}'  Mon-Fri {1}  ({2})  next={3}" -f $t.Name, $t.Time, $t.Arg, $ni)
}
