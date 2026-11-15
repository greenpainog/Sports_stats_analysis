# Registers a Windows scheduled task that runs the daily update at 10:00.
# Run it yourself once, from PowerShell:  .\scripts\register_daily_update.ps1
# Remove it again with:  Unregister-ScheduledTask -TaskName "Basketball stats daily update"
#
# -StartWhenAvailable: if the PC was off at 10:00, the task runs as soon as it
# is back on. It runs only while you are logged in (Windows' default), so it
# never needs your password. A console window flashes briefly while it runs.
$project = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute (Join-Path $project ".venv\Scripts\python.exe") `
    -Argument "-m ingestion.daily_update" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At 10:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "Basketball stats daily update" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Loads new Euroleague and Greek league games (ingestion/daily_update.py)."
