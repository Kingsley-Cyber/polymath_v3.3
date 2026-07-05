# Run ONCE as Administrator on the GPU box. After this, the extraction
# sidecars are DETERMINISTIC: they start at every boot (no login needed),
# restart within a minute if they crash, and never idle-shutdown.
#
#   .\install_windows_sidecar_tasks.ps1              # instances on 8084 + 8085
#   .\install_windows_sidecar_tasks.ps1 -Ports 8084  # single instance
#
# Remove later with:  Unregister-ScheduledTask -TaskName "PolymathGhostB_8084"
param([int[]]$Ports = @(8084, 8085))
$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run_sidecar_windows.ps1"
if (-not (Test-Path $runner)) { throw "runner not found: $runner" }

foreach ($p in $Ports) {
    $name = "PolymathGhostB_$p"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Port $p -NoIdleShutdown"
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName $name
    Write-Host "registered + started $name (boot-persistent, crash-restart, no idle shutdown)"
}

# Open the firewall so the Mac's health probes and slice dispatches reach us.
foreach ($p in $Ports) {
    $rule = "Polymath GhostB $p"
    if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP `
            -LocalPort $p -Action Allow -Profile Private | Out-Null
        Write-Host "firewall: opened TCP $p (Private profile)"
    }
}
Write-Host "`nVerify from the Mac:  curl http://<this-box-ip>:8084/health"
