$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$global:retryTestPythonw = Join-Path (Split-Path -Parent (Get-Command python).Source) 'pythonw.exe'
$global:retryTestArguments = '"' + $env:RETRY_TEST_PROXY_SCRIPT + '" --serve'

function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    [PSCustomObject]@{Actions = [PSCustomObject]@{Execute = $global:retryTestPythonw; Arguments = $global:retryTestArguments}}
}
function Invoke-RestMethod { [PSCustomObject]@{service = 'codex-transient-retry'; version = 5} }
function New-ScheduledTaskAction { [PSCustomObject]@{} }
function New-ScheduledTaskTrigger { [PSCustomObject]@{} }
function New-ScheduledTaskPrincipal { [PSCustomObject]@{} }
function New-ScheduledTaskSettingsSet { [PSCustomObject]@{} }
function Register-ScheduledTask { }
function Start-ScheduledTask { }
function codex {
    if ($env:RETRY_TEST_SCENARIO -eq 'concurrent') {
        [System.IO.File]::AppendAllText((Join-Path $env:CODEX_HOME 'config.toml'), "`n# concurrent user edit`n")
    }
    $global:LASTEXITCODE = 1
}

try {
    & $env:RETRY_TEST_ENABLE_SCRIPT
    throw 'Expected validation failure.'
} catch {
    if ($_.Exception.Message -notlike '*Codex rejected*' -and $_.Exception.Message -notlike '*configuration changed*') { throw }
}
Write-Output 'Validation failure was simulated without real task or network operations.'
