#Requires -Version 5.1
# Register TrendRadar as several STAGGERED scheduled tasks so the heavy
# Google-Trends stages do not burst against the same per-IP limit.
#
#   TrendRadar-Hourly   group=hourly   every 60 min   at :<HourlyMinute>
#   TrendRadar-Games    group=games    every 180 min  at :<GamesMinute>
#   TrendRadar-Sitemap  group=sitemap  every 1440 min at :<SitemapMinute>
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -HourlyMinute 15 -GamesMinute 0 -SitemapMinute 40
param(
    [int]$HourlyMinute = 0,
    [int]$GamesMinute = 20,
    [int]$SitemapMinute = 40
)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source
$runner = Join-Path $root "scripts\run_all.py"
if (-not (Test-Path $runner)) { throw "runner not found: $runner" }

function Register-RadarTask {
    param(
        [string]$Name, [string]$Group, [int]$Minute,
        [int]$IntervalMinutes, [string]$Description
    )
    # First fire at the next wall-clock minute equal to $Minute, then repeat.
    $start = (Get-Date).Date.AddHours((Get-Date).Hour).AddMinutes($Minute)
    while ($start -le (Get-Date)) { $start = $start.AddMinutes($IntervalMinutes) }

    $action = New-ScheduledTaskAction -Execute $python `
        -Argument "`"$runner`" --group $Group" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Once -At $start `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force -Description $Description | Out-Null
    Write-Host ("Installed {0}: group={1}, every {2} min at :{3:d2}" -f $Name, $Group, $IntervalMinutes, $Minute)
}

Register-RadarTask -Name "TrendRadar-Hourly" -Group "hourly" -Minute $HourlyMinute `
    -IntervalMinutes 60 -Description "TrendRadar: hourly hot words + enrichment (seo/signal/advice/fast)."
Register-RadarTask -Name "TrendRadar-Games" -Group "games" -Minute $GamesMinute `
    -IntervalMinutes 180 -Description "TrendRadar: new-game + game-name radar (every 3h)."
Register-RadarTask -Name "TrendRadar-Sitemap" -Group "sitemap" -Minute $SitemapMinute `
    -IntervalMinutes 1440 -Description "TrendRadar: competitor sitemaps (daily)."

Write-Host ""
Write-Host "Runs only while logged on so the proxy is available."
Write-Host "Run now : Start-ScheduledTask -TaskName TrendRadar-Hourly"
Write-Host "Status  : Get-ScheduledTask -TaskName TrendRadar-* | Get-ScheduledTaskInfo"
Write-Host "Logs    : $root\logs\run.log"
