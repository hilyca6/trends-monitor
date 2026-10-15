#Requires -Version 5.1
# Remove the TrendRadar scheduled tasks (all staggered groups).
$ErrorActionPreference = "Stop"

$taskNames = @("TrendRadar-Hourly", "TrendRadar-Games", "TrendRadar-Sitemap")
foreach ($taskName in $taskNames) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed task '$taskName'."
    } else {
        Write-Host "Task '$taskName' not found."
    }
}
