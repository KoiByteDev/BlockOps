param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Start", "Stop", "Command", "Status")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$managerDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$registryFile = Join-Path $managerDirectory "server-profiles.json"
$genericRunner = Join-Path $managerDirectory "profile-runner.ps1"

function Get-Profiles {
    return @((Get-Content -LiteralPath $registryFile -Raw | ConvertFrom-Json).profiles)
}

function Get-Listener {
    return Get-NetTCPConnection -LocalPort 25565 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Get-ActiveProfile {
    $listener = Get-Listener
    if (-not $listener) { return $null }

    $serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $parentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($serverProcess.ParentProcessId)" -ErrorAction SilentlyContinue
    $profiles = Get-Profiles

    $parentMatches = @($profiles | Where-Object {
        $parentProcess -and $parentProcess.CommandLine -and $parentProcess.CommandLine.IndexOf($_.path, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    if ($parentMatches.Count -eq 1) {
        return $parentMatches[0]
    }

    $jarMatches = @($profiles | Where-Object {
        $serverProcess.CommandLine -and $serverProcess.CommandLine.IndexOf($_.launchJar, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    if ($jarMatches.Count -eq 1) {
        return $jarMatches[0]
    }

    throw "A server is running on port 25565, but its profile could not be identified safely."
}

function Select-Profile {
    $profiles = Get-Profiles
    for ($index = 0; $index -lt $profiles.Count; $index++) {
        Write-Host "[$($index + 1)] $($profiles[$index].name)  ($($profiles[$index].minecraftVersion) $($profiles[$index].loader))"
    }
    $selection = Read-Host "Profile to start"
    $number = 0
    if (-not [int]::TryParse($selection, [ref]$number) -or $number -lt 1 -or $number -gt $profiles.Count) {
        throw "Invalid profile selection."
    }
    return $profiles[$number - 1]
}

function Start-Profile($Profile) {
    if (Get-Listener) { throw "A Minecraft profile is already running. Stop it before starting another." }
    if (-not (Test-Path -LiteralPath $Profile.path)) { throw "Profile folder is missing: $($Profile.path)" }

    if ($Profile.runner -eq "existing") {
        $startScript = Join-Path $Profile.path "start-server.bat"
        Start-Process -FilePath "C:\Windows\System32\cmd.exe" -ArgumentList '/c',('"' + $startScript + '"') -WorkingDirectory $Profile.path -WindowStyle Hidden
    }
    else {
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$genericRunner`" -ProfilePath `"$($Profile.path)`""
        Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $Profile.path -WindowStyle Hidden
    }

    & "C:\Program Files\playit_gg\bin\playit.exe" start | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    do { Start-Sleep -Seconds 1; $listener = Get-Listener } while (-not $listener -and (Get-Date) -lt $deadline)
    if (-not $listener) { throw "$($Profile.name) did not open port 25565 within 90 seconds." }
    Write-Host "$($Profile.name) is online. Playit is running." -ForegroundColor Green
}

function Stop-Profile($Profile) {
    $controlDirectory = Join-Path $Profile.path ".control"
    New-Item -ItemType Directory -Path $controlDirectory -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $controlDirectory "stop.request") -Value "universal-stop"

    $deadline = (Get-Date).AddSeconds(120)
    do { Start-Sleep -Seconds 1; $listener = Get-Listener } while ($listener -and (Get-Date) -lt $deadline)
    if ($listener) { throw "$($Profile.name) did not stop within 120 seconds." }
    & "C:\Program Files\playit_gg\bin\playit.exe" stop | Out-Null
    Write-Host "$($Profile.name) and Playit are fully stopped." -ForegroundColor Green
}

function Send-ProfileCommand($Profile) {
    $command = (Read-Host "server command").Trim().TrimStart('/')
    if (-not $command) { return }

    $controlDirectory = Join-Path $Profile.path ".control"
    $request = Join-Path $controlDirectory "command.request"
    $pending = Join-Path $controlDirectory "command.pending"
    New-Item -ItemType Directory -Path $controlDirectory -Force | Out-Null

    $deadline = (Get-Date).AddSeconds(10)
    while ((Test-Path -LiteralPath $request) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    if (Test-Path -LiteralPath $request) { throw "The previous command is still pending." }

    [IO.File]::WriteAllText($pending, $command, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $pending -Destination $request -Force
    $deadline = (Get-Date).AddSeconds(10)
    while ((Test-Path -LiteralPath $request) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    if (Test-Path -LiteralPath $request) { throw "The active profile did not collect the command." }

    Start-Sleep -Milliseconds 750
    Write-Host "Command submitted to $($Profile.name)." -ForegroundColor Green
    $latestLog = Join-Path $Profile.path "logs\latest.log"
    if (Test-Path -LiteralPath $latestLog) {
        Write-Host "Recent server output:" -ForegroundColor DarkCyan
        Get-Content -LiteralPath $latestLog -Tail 8
    }
}

try {
    switch ($Action) {
        "Start" {
            if (Get-Listener) {
                $active = Get-ActiveProfile
                Write-Host "$($active.name) is already running." -ForegroundColor Yellow
            } else {
                Start-Profile (Select-Profile)
            }
        }
        "Stop" {
            $active = Get-ActiveProfile
            if (-not $active) { Write-Host "No Minecraft profile is running." -ForegroundColor Yellow }
            else { Stop-Profile $active }
        }
        "Command" {
            $active = Get-ActiveProfile
            if (-not $active) { Write-Host "No Minecraft profile is running." -ForegroundColor Yellow }
            else { Send-ProfileCommand $active }
        }
        "Status" {
            $active = Get-ActiveProfile
            if (-not $active) { Write-Host "No Minecraft profile is running." }
            else { Write-Host "Active profile: $($active.name)"; Write-Host "Folder: $($active.path)" }
        }
    }
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
