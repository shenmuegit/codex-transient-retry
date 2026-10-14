$ErrorActionPreference = 'Stop'
$retryScript = Join-Path $PSScriptRoot 'retry_proxy.py'
$retryPython = (Get-Command python -ErrorAction Stop).Source
$retryPythonw = Join-Path (Split-Path -Parent $retryPython) 'pythonw.exe'
$retryArguments = '"' + $retryScript + '" --serve'
$retryTaskName = 'CodexTransientRetry'
$retryNeedsRestart = $false
$retryHealth = $null

if (-not (Test-Path -LiteralPath $retryPythonw)) {
    throw 'Python 3.11 or newer, including pythonw.exe, is required.'
}

$retryExistingTask = Get-ScheduledTask -TaskName $retryTaskName -ErrorAction SilentlyContinue
if ($retryExistingTask -and (($retryExistingTask.Actions.Execute -ne $retryPythonw) -or ($retryExistingTask.Actions.Arguments -ne $retryArguments))) {
    throw 'A different scheduled task already uses the name CodexTransientRetry.'
}

try {
    $retryHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:18761/health' -TimeoutSec 2
    if ($retryHealth.service -ne 'codex-transient-retry') {
        throw 'Port 18761 is already used by another service.'
    }
    $retryNeedsRestart = $retryHealth.version -ne 6
} catch {
    $retryListener = Get-NetTCPConnection -LocalPort 18761 -State Listen -ErrorAction SilentlyContinue
    if ($retryListener) {
        throw 'Port 18761 is occupied and does not respond as the Codex retry helper.'
    }
}

if ($retryHealth -and -not $retryExistingTask) {
    throw 'A manually started retry gateway is running. Stop it before enabling the scheduled task.'
}

if ($retryNeedsRestart) {
    if (Get-NetTCPConnection -LocalPort 18761 -State Established -ErrorAction SilentlyContinue) {
        throw 'The retry gateway has active requests. Wait for them to finish, then run Enable.ps1 again.'
    }
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18761/shutdown' -Headers @{'X-Codex-Retry-Control' = 'stop'} -TimeoutSec 3 | Out-Null
    Start-Sleep -Milliseconds 700
}

$retryIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$retryAction = New-ScheduledTaskAction -Execute $retryPythonw -Argument $retryArguments -WorkingDirectory $PSScriptRoot
$retryTrigger = New-ScheduledTaskTrigger -AtLogOn -User $retryIdentity
$retryPrincipal = New-ScheduledTaskPrincipal -UserId $retryIdentity -LogonType Interactive -RunLevel Limited
$retrySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $retryTaskName -Action $retryAction -Trigger $retryTrigger -Principal $retryPrincipal -Settings $retrySettings -Description 'Local buffered retries for transient Codex inference and compaction failures.' -Force | Out-Null
Start-ScheduledTask -TaskName $retryTaskName

$retryReady = $false
for ($retryCheck = 0; $retryCheck -lt 40; $retryCheck++) {
    try {
        $retryHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:18761/health' -TimeoutSec 1
        if (($retryHealth.service -eq 'codex-transient-retry') -and ($retryHealth.version -eq 6)) {
            $retryReady = $true
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 250
}
if (-not $retryReady) {
    throw 'The retry helper did not start; Codex configuration has not been changed.'
}

$retryApplyOutput = & $retryPython $retryScript --configure enable
if ($LASTEXITCODE -ne 0) {
    throw 'Could not apply the retry provider; the previous configuration was retained.'
}
$retryApply = $retryApplyOutput | ConvertFrom-Json
& codex features list | Out-Null
if ($LASTEXITCODE -ne 0) {
    if (-not $retryApply.unchanged) {
        $retryConfigHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex' }
        $retryConfigPath = Join-Path $retryConfigHome 'config.toml'
        $retryCurrentHash = & $retryPython -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' $retryConfigPath
        if ($LASTEXITCODE -ne 0 -or $retryCurrentHash -ne $retryApply.config_sha256) {
            throw 'Codex validation failed and configuration changed meanwhile; it was left untouched.'
        }
        $retryRollbackTemp = $retryConfigPath + '.retry-rollback-' + [Guid]::NewGuid().ToString('N') + '.tmp'
        try {
            Copy-Item -LiteralPath $retryApply.operation_backup -Destination $retryRollbackTemp
            [System.IO.File]::Replace($retryRollbackTemp, $retryConfigPath, [NullString]::Value)
        } finally {
            if (Test-Path -LiteralPath $retryRollbackTemp) { Remove-Item -LiteralPath $retryRollbackTemp }
        }
    }
    throw 'Codex rejected the retry provider; the prior configuration was preserved or restored.'
}

Write-Output 'Enabled: localhost port 18761; retry delays 30, 60, 120 seconds; at most 3 retries.'
Write-Output 'The helper starts automatically at Windows sign-in.'
Write-Output 'The default provider stays OpenAI. Model catalog I/O timeout: 3 seconds; inference retry budget is unchanged.'
Write-Output 'Restart Codex when convenient to reload existing connections.'
