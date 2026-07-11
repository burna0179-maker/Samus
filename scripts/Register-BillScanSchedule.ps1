<#
.SYNOPSIS
  Register the Gmail bill-scan tasks: weekly cadence + on-demand payment
  validation.

.DESCRIPTION
  Idempotent. Creates/refreshes two tasks running Scan-GmailBills.ps1
  (read-only Gmail scan -> bills snapshot vs CODB registry):

    'Samus Bill Scan Weekly'      Mondays 08:30 - routine weekly snapshot so
                                  declined payments / new bills surface at the
                                  start of the week.
    'Samus Bill Scan Validate'    NO trigger (on-demand). Operator fires it
                                  after paying bills (e.g. when a closed deal
                                  funds remediation) to VALIDATE the payments
                                  actually cleared:
                                    Start-ScheduledTask -TaskName 'Samus Bill Scan Validate'
                                  Uses a 14-day lookback so the run is fast and
                                  focused on the just-paid cycle.

  Runs as the current interactive user (Limited) so the DPAPI Gmail secrets
  decrypt. The scan never modifies the mailbox or the CODB registry.
#>
[CmdletBinding()]
param(
    [Parameter()][string]$WeeklyTime = '08:30'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$scan = Join-Path $here 'Scan-GmailBills.ps1'
if (-not (Test-Path $scan)) { throw "Scan-GmailBills.ps1 not found at $scan" }

$me        = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
                -MultipleInstances IgnoreNew -Compatibility Win8

# Weekly Monday scan (90-day lookback, default snapshot path)
$name = 'Samus Bill Scan Weekly'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"& '$scan'; exit `$LASTEXITCODE`"" `
    -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $WeeklyTime
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Weekly Gmail bill scan: declined payments + bill deltas vs CODB registry. Read-only.' | Out-Null
Write-Host ("Registered '{0}'  Mondays {1}  next={2}" -f $name, $WeeklyTime, (Get-ScheduledTaskInfo -TaskName $name).NextRunTime)

# On-demand validation scan (no trigger; operator fires after paying bills)
$name = 'Samus Bill Scan Validate'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command `"& '$scan' -LookbackDays 14; exit `$LASTEXITCODE`"" `
    -WorkingDirectory $here
Register-ScheduledTask -TaskName $name -Action $action `
    -Principal $principal -Settings $settings `
    -Description 'On-demand post-payment validation scan (14-day lookback). Fire after paying bills: Start-ScheduledTask -TaskName ''Samus Bill Scan Validate''' | Out-Null
Write-Host "Registered '$name'  (on-demand - fire after payments with: Start-ScheduledTask -TaskName '$name')"
