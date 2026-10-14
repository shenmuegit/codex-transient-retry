$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$global:retryTestConfigChanges = 0

function Get-Command {
    [CmdletBinding()] param([string]$Name)
    [PSCustomObject]@{Source = 'Invoke-RetryTestPython'}
}
function Invoke-RetryTestPython {
    $global:retryTestConfigChanges++
    $global:LASTEXITCODE = 0
}
function codex { $global:LASTEXITCODE = 0 }
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    [PSCustomObject]@{Actions = [PSCustomObject]@{Arguments = '"C:\another-install\retry_proxy.py" --serve'}}
}

try {
    & $env:RETRY_TEST_DISABLE_SCRIPT
    throw 'Expected the installation guard to reject this task.'
} catch {
    if ($_.Exception.Message -notlike '*belongs to another installation*') { throw }
}
if ($global:retryTestConfigChanges -ne 0) {
    throw 'Configuration was changed before checking installation ownership.'
}
Write-Output 'Ownership rejection preserved the configuration.'
