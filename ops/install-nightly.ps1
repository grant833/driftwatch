# One-time setup: registers the "driftwatch nightly" scheduled task (runs as you, daily
# at 5:30 PM, and catches up when the PC wakes if it was asleep at that time).
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File ops\install-nightly.ps1

$Script = Join-Path $PSScriptRoot "nightly.ps1"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$Trigger = New-ScheduledTaskTrigger -Daily -At "5:30PM"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "driftwatch nightly" -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Anchor the driftwatch ledger and publish dashboard data" `
    -Force | Out-Null
Write-Output "Registered 'driftwatch nightly' (daily 5:30 PM). Test it now with:"
Write-Output "  Start-ScheduledTask -TaskName 'driftwatch nightly'; Start-Sleep 90; Get-Content ops\nightly.log -Tail 20"
