<#
.SYNOPSIS
  Register the two Windows Scheduled Tasks that duty-cycle the Samus Cloud Run
  fleet for off-hours cost control.

.DESCRIPTION
  Idempotent. Creates/refreshes:
    'Samus Cloud ScaleUp'    Mon-Fri 06:45 PT -> Set-SamusCloudDutyCycle -Min 1
    'Samus Cloud ScaleDown'  Mon-Fri 19:00 PT -> Set-SamusCloudDutyCycle -Min 0
  Runs as the current interactive user (Limited) so the profile's gcloud auth
  (project ${GCP_PROJECT}) is available. Weekends have no ScaleUp, so the fleet
  stays scaled-to-zero from Fri 19:00 to Mon 06:45. The 18:30 EOD Cloud
  Scheduler job stays daily and cold-starts the gateway just for the report.

.PARAMETER UpTime / DownTime
  Override the daily trigger times ('HH:mm'). Defaults 06:45 / 19:00.
#>
[CmdletBinding()]
param(
    [Parameter()][string]$UpTime   = '06:45',
    [Parameter()][string]$DownTime = '19:00'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'Set-SamusCloudDutyCycle.ps1'
if (-not (Test-Path $script)) { throw "Set-SamusCloudDutyCycle.ps1 not found at $script" }

$me        = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
                -MultipleInstances IgnoreNew -Compatibility Win8
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$days      = @('Monday','Tuesday','Wednesday','Thursday','Friday')

$tasks = @(
    @{ Name = 'Samus Cloud ScaleUp';   Time = $UpTime;   Min = 1 },
    @{ Name = 'Samus Cloud ScaleDown'; Time = $DownTime; Min = 0 }
)

foreach ($t in $tasks) {
    if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }
    $cmd = "& '$script' -Min $($t.Min); exit `$LASTEXITCODE"
    $arg     = "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $here
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $t.Time
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "Samus Cloud Run duty-cycle: min-instances=$($t.Min) on the 12 always-on services (off-hours cost control)." | Out-Null
    $ni = (Get-ScheduledTaskInfo -TaskName $t.Name).NextRunTime
    Write-Host ("Registered '{0}'  Mon-Fri {1}  (min={2})  next={3}" -f $t.Name, $t.Time, $t.Min, $ni)
}

Write-Host ""
Write-Host "Run as: $me (Limited / Interactive)"
Write-Host "Manual test:  .\Set-SamusCloudDutyCycle.ps1 -Min 0   (scale down now)"
