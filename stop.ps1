param([double] $DelaySeconds = 0)

if ($DelaySeconds -gt 0) { Start-Sleep -Milliseconds ([Math]::Ceiling($DelaySeconds * 1000)) }
$projectRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path).TrimEnd('\')
$all = @(Get-CimInstance Win32_Process)
$targets = [Collections.Generic.HashSet[int]]::new()
foreach ($process in $all) {
    $id = [int]$process.ProcessId
    if ($id -eq [int]$PID) { continue }
    $inProject = $process.ExecutablePath -and $process.ExecutablePath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)
    if ($inProject) { [void]$targets.Add($id) }
}

do {
    $added = $false
    foreach ($process in $all) {
        $id = [int]$process.ProcessId
        if ($id -ne [int]$PID -and $targets.Contains([int]$process.ParentProcessId) -and $targets.Add($id)) { $added = $true }
    }
} while ($added)

foreach ($id in $targets) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
