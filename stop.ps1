param(
    [ValidateRange(0, 3600)][double] $DelaySeconds = 0,
    [ValidateRange(0, 60)][double] $TimeoutSeconds = 10,
    [switch] $Force
)

function Get-VerifiedDesktopProcess {
    param($Record, [string] $Root)
    if ($Record.kind -ne 'live-subtitle-translator' -or
        $Record.root -ne [IO.Path]::GetFullPath($Root).TrimEnd('\') -or
        $Record.event_name -notmatch '^Local\\LiveSubtitleTranslator-[0-9a-f]{32}$') { return $null }
    $process = $null
    try {
        $process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop
        # Hold the original process handle, including while waiting. A reused
        # PID cannot redirect the final Kill() to a different process.
        $null = $process.Handle
        if ($process.Path -ne $Record.executable -or
            $process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -ne $Record.created_filetime) {
            $process.Dispose()
            return $null
        }
        return $process
    } catch {
        if ($null -ne $process) { $process.Dispose() }
        return $null
    }
}

function Stop-TranslatorApplication {
    param([string] $Root, [double] $WaitSeconds = 10, [switch] $ForceStop)
    $recordPath = Join-Path $Root '.runtime\desktop-process.json'
    try { $record = Get-Content -LiteralPath $recordPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
    catch { Write-Host 'No running desktop instance is registered in this folder.'; return 0 }
    $process = Get-VerifiedDesktopProcess -Record $record -Root $Root
    if ($null -eq $process) { Write-Host 'The desktop record is stale or does not match the running process.'; return 0 }
    try {
        try {
            $signal = [Threading.EventWaitHandle]::OpenExisting($record.event_name)
            try { $null = $signal.Set() } finally { $signal.Dispose() }
        } catch {
            Write-Host 'The desktop stop signal is unavailable.'
        }
        if ($process.WaitForExit([int]([Math]::Ceiling($WaitSeconds * 1000)))) { return 0 }
        if ($ForceStop) {
            $process.Kill()
            if ($process.WaitForExit(5000)) { return 0 }
        }
        Write-Host 'The verified desktop is still running. Use stop.ps1 -Force to force only this application to exit.'
        return 1
    } finally { $process.Dispose() }
}

# Dot-sourcing exposes the identity check for offline tests without stopping anything.
if ($MyInvocation.InvocationName -ne '.') {
    if ($DelaySeconds -gt 0) { Start-Sleep -Milliseconds ([Math]::Ceiling($DelaySeconds * 1000)) }
    exit (Stop-TranslatorApplication -Root $PSScriptRoot -WaitSeconds $TimeoutSeconds -ForceStop:$Force)
}
