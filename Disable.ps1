$ErrorActionPreference = 'Stop'
$retryScript = Join-Path $PSScriptRoot 'retry_proxy.py'
$retryPython = (Get-Command python -ErrorAction Stop).Source

$retryTask = Get-ScheduledTask -TaskName 'CodexTransientRetry' -ErrorAction SilentlyContinue
if ($retryTask) {
    $retryExpectedArguments = '"' + $retryScript + '" --serve'
    if ($retryTask.Actions.Arguments -ne $retryExpectedArguments) {
        throw 'The scheduled task belongs to another installation; configuration was not changed.'
    }
}

& $retryPython $retryScript --configure disable
if ($LASTEXITCODE -ne 0) {
    throw 'Configuration could not be restored; the helper remains running.'
}
& codex features list | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Codex configuration needs attention; the helper remains running.'
}
if ($retryTask) {
    Unregister-ScheduledTask -TaskName 'CodexTransientRetry' -Confirm:$false
}
try {
    $retryHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:18761/health' -TimeoutSec 2
    if ($retryHealth.service -eq 'codex-transient-retry') {
        Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18761/shutdown' -Headers @{'X-Codex-Retry-Control' = 'stop'} -TimeoutSec 3 | Out-Null
    }
} catch {
    Write-Warning 'Configuration was restored. The retry helper was already stopped or is finishing a request.'
}
Write-Output 'Disabled. Restart Codex when convenient to load the original provider.'
