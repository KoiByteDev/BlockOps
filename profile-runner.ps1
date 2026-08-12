param(
    [Parameter(Mandatory = $true)]
    [string]$ProfilePath
)

$ErrorActionPreference = "Stop"
$ProfilePath = [System.IO.Path]::GetFullPath($ProfilePath)
$profileFile = Join-Path $ProfilePath "profile.json"
if (-not (Test-Path -LiteralPath $profileFile)) {
    throw "Profile configuration not found: $profileFile"
}

$profile = Get-Content -LiteralPath $profileFile -Raw | ConvertFrom-Json
$controlDirectory = Join-Path $ProfilePath ".control"
$stopRequest = Join-Path $controlDirectory "stop.request"
$commandRequest = Join-Path $controlDirectory "command.request"
$runnerLog = Join-Path $ProfilePath "profile-runner.log"
New-Item -ItemType Directory -Path $controlDirectory -Force | Out-Null

$mutexName = "MinecraftProfile-" + ($profile.id -replace '[^A-Za-z0-9_-]', '_')
$createdNew = $false
$runnerMutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) { exit 0 }

function Write-RunnerLog([string]$Message) {
    Add-Content -LiteralPath $runnerLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
}

function Start-ProfileProcess {
    if (-not (Test-Path -LiteralPath $profile.javaPath)) {
        throw "Java executable not found: $($profile.javaPath)"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $ProfilePath $profile.launchJar))) {
        throw "Server launcher not found: $($profile.launchJar)"
    }

    $arguments = @($profile.jvmArguments) + @("-Xms$($profile.minimumRam)", "-Xmx$($profile.maximumRam)", "-jar", $profile.launchJar) + @($profile.serverArguments)
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $profile.javaPath
    $startInfo.Arguments = (($arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' ')
    $startInfo.WorkingDirectory = $ProfilePath
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    # The runner remains hidden and owns stdin; the profile manager streams
    # logs/latest.log without tying server lifetime to a visible console.
    $startInfo.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Failed to start profile $($profile.name)." }
    Write-RunnerLog "Started $($profile.name) with process ID $($process.Id)."
    return $process
}

try {
    Set-Location -LiteralPath $ProfilePath
    if (Test-Path -LiteralPath $stopRequest) { Remove-Item -LiteralPath $stopRequest -Force }

    while ($true) {
        $serverProcess = Start-ProfileProcess
        $requestedStop = $false

        while (-not $serverProcess.HasExited) {
            if (Test-Path -LiteralPath $commandRequest) {
                $commands = @(Get-Content -LiteralPath $commandRequest)
                Remove-Item -LiteralPath $commandRequest -Force
                foreach ($command in $commands) {
                    $command = $command.Trim().TrimStart('/')
                    if ($command) {
                        $serverProcess.StandardInput.WriteLine($command)
                        $serverProcess.StandardInput.Flush()
                        Write-RunnerLog "Submitted command: $command"
                    }
                }
            }

            if (Test-Path -LiteralPath $stopRequest) {
                Remove-Item -LiteralPath $stopRequest -Force
                $requestedStop = $true
                $serverProcess.StandardInput.WriteLine("save-all flush")
                $serverProcess.StandardInput.WriteLine("stop")
                $serverProcess.StandardInput.Flush()
                if (-not $serverProcess.WaitForExit(120000)) { $serverProcess.Kill() }
                break
            }
            Start-Sleep -Milliseconds 500
        }

        if ($requestedStop) { break }
        Write-RunnerLog "Server exited unexpectedly; restarting in 10 seconds."
        Start-Sleep -Seconds 10
    }
}
finally {
    if ($createdNew) { $runnerMutex.ReleaseMutex() }
    $runnerMutex.Dispose()
}
