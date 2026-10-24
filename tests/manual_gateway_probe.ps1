$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$global:retryTestRegistrations = 0
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    $null
}
function Invoke-RestMethod { [PSCustomObject]@{service = 'codex-transient-retry'; version = 12} }
function New-ScheduledTaskAction { [PSCustomObject]@{} }
function New-ScheduledTaskTrigger { [PSCustomObject]@{} }
function New-ScheduledTaskPrincipal { [PSCustomObject]@{} }
function New-ScheduledTaskSettingsSet { [PSCustomObject]@{} }
function Register-ScheduledTask { $global:retryTestRegistrations++ }
function Start-ScheduledTask { }
function codex { $global:LASTEXITCODE = 0 }
try {
    & $env:RETRY_TEST_ENABLE_SCRIPT
    throw 'Expected manual gateway rejection.'
} catch {
    if ($_.Exception.Message -notlike '*manually started*') { throw }
}
if ($global:retryTestRegistrations -ne 0) { throw 'A task was registered while a manual gateway owned the port.' }
Write-Output 'Manual gateway was left untouched.'
