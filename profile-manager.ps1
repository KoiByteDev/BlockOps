$ErrorActionPreference = "Stop"

$managerDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$registryFile = Join-Path $managerDirectory "server-profiles.json"
$genericRunner = Join-Path $managerDirectory "profile-runner.ps1"
$defaultProfilesDirectory = Join-Path $managerDirectory "profiles"
$javaRuntimesDirectory = Join-Path $managerDirectory "runtimes\java"
$installCacheDirectory = Join-Path $managerDirectory "install-cache"
$playitExecutable = "C:\Program Files\playit_gg\bin\playit.exe"
$publicServerAddress = "jacobs-favourites.tun.ply.gg"

# A PowerShell script is loaded into memory when its window opens. Prevent two
# stale manager windows from issuing competing profile and Playit operations.
$managerMutexCreated = $false
$managerMutex = [System.Threading.Mutex]::new($true, "MinecraftServerProfileManager", [ref]$managerMutexCreated)
if (-not $managerMutexCreated) {
    Write-Host "Another Server Manager window is already open. Close it before opening a new one." -ForegroundColor Yellow
    $managerMutex.Dispose()
    exit 1
}

function Read-Registry {
    if (-not (Test-Path -LiteralPath $registryFile)) { throw "Profile registry is missing." }
    return Get-Content -LiteralPath $registryFile -Raw | ConvertFrom-Json
}

function Save-Registry($Registry) {
    $Registry | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $registryFile -Encoding UTF8
}

function Get-Profiles {
    return @(Read-Registry).profiles
}

function Select-Profile([string]$Prompt = "Select profile") {
    $profiles = @(Get-Profiles)
    if ($profiles.Count -eq 0) { Write-Host "No profiles exist." -ForegroundColor Yellow; return $null }
    Write-Host ""
    for ($i = 0; $i -lt $profiles.Count; $i++) {
        Write-Host "[$($i + 1)] $($profiles[$i].name)  ($($profiles[$i].minecraftVersion) $($profiles[$i].loader))"
    }
    $choice = Read-Host $Prompt
    $number = 0
    if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $profiles.Count) {
        Write-Host "Invalid selection." -ForegroundColor Red
        return $null
    }
    return $profiles[$number - 1]
}

function Test-RamValue([string]$Value) {
    return $Value -match '^[1-9][0-9]*[MG]$'
}

function Get-ListeningServer {
    return Get-NetTCPConnection -LocalPort 25565 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Start-PlayitService {
    if (-not (Test-Path -LiteralPath $playitExecutable)) { throw "Playit is not installed at $playitExecutable" }
    & $playitExecutable start | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Playit failed to start." }
}

function Stop-PlayitService {
    if (-not (Test-Path -LiteralPath $playitExecutable)) { return }
    & $playitExecutable stop | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Playit failed to stop." }
}

function Wait-ForProfileStartup($Profile, [int]$InitialLogLineCount, [int]$TimeoutSeconds = 900) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $latestLog = Join-Path $Profile.path "logs\latest.log"
    $seenLineCount = $InitialLogLineCount
    $nextHeartbeat = (Get-Date).AddSeconds(10)
    $readyMessageSeen = $false
    while ((Get-Date) -lt $deadline) {
        $listener = Get-ListeningServer
        if (Test-Path -LiteralPath $latestLog) {
            $lines = @(Get-Content -LiteralPath $latestLog -ErrorAction SilentlyContinue)
            if ($lines.Count -lt $seenLineCount) { $seenLineCount = 0 }
            if ($lines.Count -gt $seenLineCount) {
                $newLines = @($lines | Select-Object -Skip $seenLineCount)
                foreach ($line in $newLines) {
                    Write-Host $line
                    if ($line -match '\bDone \([0-9.,]+s\)!') { $readyMessageSeen = $true }
                }
                $seenLineCount = $lines.Count
            }
        }
        if ($listener -and $readyMessageSeen) { return $listener }
        if ((Get-Date) -ge $nextHeartbeat) {
            Write-Host "Still loading $($Profile.name); waiting for Minecraft's Done message..." -ForegroundColor DarkCyan
            $nextHeartbeat = (Get-Date).AddSeconds(10)
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$($Profile.name) did not open port 25565 within $TimeoutSeconds seconds. Review its visible server console and logs."
}

function Wait-ForProfileShutdown([int]$TimeoutSeconds = 120) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-ListeningServer) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
    }
    if (Get-ListeningServer) { throw "Minecraft did not stop within $TimeoutSeconds seconds." }
}

function Get-RequiredJavaMajor([string]$MinecraftVersion) {
    if ($MinecraftVersion -match '^(\d{2})\.') {
        if ([int]$Matches[1] -ge 26) { return 25 }
    }
    if ($MinecraftVersion -notmatch '^(\d+)\.(\d+)(?:\.(\d+))?') {
        throw "Cannot determine the Java requirement for Minecraft $MinecraftVersion."
    }
    $minor = [int]$Matches[2]
    $patch = if ($Matches[3]) { [int]$Matches[3] } else { 0 }
    if ($minor -le 16) { return 8 }
    if ($minor -eq 17) { return 16 }
    if ($minor -le 19 -or ($minor -eq 20 -and $patch -le 4)) { return 17 }
    return 21
}

function Get-JavaMajor([string]$JavaPath) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $JavaPath
    $startInfo.Arguments = '-version'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $output = $process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $line = [string]($output -split "`r?`n" | Select-Object -First 1)
    if ($line -notmatch 'version\s+"(?:(1)\.)?(\d+)') { return $null }
    if ($Matches[1]) { return [int]$Matches[2] }
    return [int]$Matches[2]
}

function Resolve-JavaPath([int]$RequiredMajor) {
    $roots = @(
        "C:\Program Files\Eclipse Adoptium",
        "C:\Program Files\Java",
        "C:\Program Files\Microsoft",
        $javaRuntimesDirectory
    )
    $candidates = @()
    foreach ($root in $roots) {
        if (Test-Path -LiteralPath $root) {
            $candidates += Get-ChildItem -LiteralPath $root -Filter java.exe -Recurse -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        }
    }
    $match = $candidates | Where-Object { (Get-JavaMajor $_) -eq $RequiredMajor } | Select-Object -First 1
    if ($match) { return $match }

    Write-Host "Java $RequiredMajor is not installed. Downloading a private Temurin runtime..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $javaRuntimesDirectory -Force | Out-Null
    $runtimeFolder = Join-Path $javaRuntimesDirectory "temurin-$RequiredMajor"
    $archive = Join-Path $javaRuntimesDirectory "temurin-$RequiredMajor.zip"
    Invoke-WebRequest "https://api.adoptium.net/v3/binary/latest/$RequiredMajor/ga/windows/x64/jre/hotspot/normal/eclipse" -OutFile $archive
    if (Test-Path -LiteralPath $runtimeFolder) { Remove-Item -LiteralPath $runtimeFolder -Recurse -Force }
    New-Item -ItemType Directory -Path $runtimeFolder | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeFolder -Force
    Remove-Item -LiteralPath $archive -Force
    $java = Get-ChildItem -LiteralPath $runtimeFolder -Filter java.exe -Recurse | Select-Object -First 1 -ExpandProperty FullName
    if (-not $java -or (Get-JavaMajor $java) -ne $RequiredMajor) { throw "The Java $RequiredMajor runtime installation could not be verified." }
    return $java
}

function Assert-MinecraftVersion([string]$MinecraftVersion) {
    $manifest = Invoke-RestMethod "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
    $entry = $manifest.versions | Where-Object id -eq $MinecraftVersion | Select-Object -First 1
    if (-not $entry) { throw "Minecraft version '$MinecraftVersion' does not exist in Mojang's version manifest." }
}

function Start-SelectedProfile {
    if (Get-ListeningServer) {
        Write-Host "Port 25565 is already in use. Stop the active profile first." -ForegroundColor Yellow
        return
    }
    $profile = Select-Profile "Profile to start"
    if (-not $profile) { return }
    if (-not (Test-Path -LiteralPath $profile.path)) { Write-Host "Profile folder is missing." -ForegroundColor Red; return }

    $latestLog = Join-Path $profile.path "logs\latest.log"
    $initialLogLineCount = if (Test-Path -LiteralPath $latestLog) { @(Get-Content -LiteralPath $latestLog).Count } else { 0 }

    Write-Host ""
    Write-Host "Starting $($profile.name)..." -ForegroundColor Cyan
    Write-Host "This screen will remain here and stream startup until the server is ready." -ForegroundColor Cyan
    if ($profile.runner -eq "existing") {
        $script = Join-Path $profile.path "start-server.bat"
        Start-Process -FilePath "C:\Windows\System32\cmd.exe" -ArgumentList '/c',('"' + $script + '"') -WorkingDirectory $profile.path -WindowStyle Hidden
    } else {
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$genericRunner`" -ProfilePath `"$($profile.path)`""
        # The runner owns Java's stdin and must survive independently of this
        # manager window. Startup output is streamed here from latest.log.
        Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $profile.path -WindowStyle Hidden
    }
    Write-Host "Starting Playit agent..." -ForegroundColor Cyan
    Start-PlayitService
    $listener = Wait-ForProfileStartup $profile $initialLogLineCount
    $playitStatus = & $playitExecutable status 2>&1
    if (-not ($playitStatus -match 'Phase:\s+running')) { throw "Minecraft started, but the Playit agent is not running." }
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "$($profile.name) is online (PID $($listener.OwningProcess))." -ForegroundColor Green
    Write-Host "JOIN ADDRESS: $publicServerAddress" -ForegroundColor Green
    Write-Host "MINECRAFT PORT: 25565 (handled automatically by the Playit address)" -ForegroundColor Green
    Write-Host "Paste the join address into Minecraft; no extra port is needed." -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Read-Host "Press Enter to return to the Server Manager menu"
}

function Stop-ActiveProfile {
    $listener = Get-ListeningServer
    if (-not $listener) {
        Stop-PlayitService
        Write-Host "No Minecraft server was running. Playit and its agent are stopped." -ForegroundColor Green
        return
    }
    $javaProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $profile = Get-Profiles | Where-Object { $javaProcess.CommandLine -like "*$($_.launchJar)*" -and (Test-Path -LiteralPath $_.path) } | Select-Object -First 1
    if (-not $profile) { Write-Host "Could not identify the active profile safely." -ForegroundColor Red; return }
    $runnerPattern = if ($profile.runner -eq 'existing') { 'server-manager\.ps1' } else { 'profile-runner\.ps1' }
    $activeRunner = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match $runnerPattern -and $_.CommandLine.IndexOf($profile.path, [StringComparison]::OrdinalIgnoreCase) -ge 0
    } | Select-Object -First 1
    if (-not $activeRunner) {
        Write-Host "$($profile.name) is orphaned, so a graceful console stop cannot be delivered." -ForegroundColor Yellow
        Write-Host "A forced stop can lose changes made since Minecraft's most recent automatic save." -ForegroundColor Yellow
        $confirmation = (Read-Host "Type FORCE STOP to terminate Minecraft and Playit, or press Enter to cancel").Trim()
        if ($confirmation -cne 'FORCE STOP') {
            Write-Host "Forced stop cancelled. Nothing was changed." -ForegroundColor Cyan
            return
        }
        Stop-Process -Id $listener.OwningProcess -Force
        Wait-ForProfileShutdown 30
        Stop-PlayitService
        Write-Host "$($profile.name), Playit, and the Playit agent were force-stopped." -ForegroundColor Green
        return
    }
    $control = Join-Path $profile.path ".control"
    New-Item -ItemType Directory -Path $control -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $control "stop.request") -Value "profile-manager"
    Write-Host "Saving and stopping $($profile.name)..." -ForegroundColor Cyan
    Wait-ForProfileShutdown
    Stop-PlayitService
    Write-Host "$($profile.name), Playit, and the Playit agent are fully stopped." -ForegroundColor Green
}

function Send-ServerCommand {
    $listener = Get-ListeningServer
    if (-not $listener) { Write-Host "No active server." -ForegroundColor Yellow; return }
    $profile = Select-Profile "Active profile receiving the command"
    if (-not $profile) { return }
    $command = (Read-Host "server command").Trim().TrimStart('/')
    if (-not $command) { return }
    $control = Join-Path $profile.path ".control"
    New-Item -ItemType Directory -Path $control -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $control "command.request") -Value $command -NoNewline
    Write-Host "Command queued." -ForegroundColor Green
}

function Install-VanillaServer([string]$Folder, [string]$MinecraftVersion) {
    $manifest = Invoke-RestMethod "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
    $entry = $manifest.versions | Where-Object id -eq $MinecraftVersion | Select-Object -First 1
    if (-not $entry) { throw "Minecraft version $MinecraftVersion was not found." }
    $details = Invoke-RestMethod $entry.url
    New-Item -ItemType Directory -Path $installCacheDirectory -Force | Out-Null
    $cachedServer = Join-Path $installCacheDirectory "minecraft-$MinecraftVersion-server.jar"
    if (-not (Test-Path -LiteralPath $cachedServer)) {
        Invoke-WebRequest $details.downloads.server.url -OutFile $cachedServer
    }
    Copy-Item -LiteralPath $cachedServer -Destination (Join-Path $Folder "server.jar") -Force
    return "server.jar"
}

function Install-FabricServer([string]$Folder, [string]$MinecraftVersion, [string]$LoaderVersion) {
    if (-not $LoaderVersion) {
        $LoaderVersion = ((Invoke-RestMethod "https://meta.fabricmc.net/v2/versions/loader/$MinecraftVersion") | Where-Object { $_.loader.stable } | Select-Object -First 1).loader.version
    }
    if (-not $LoaderVersion) { throw "No stable Fabric loader supports Minecraft $MinecraftVersion." }
    $installer = ((Invoke-RestMethod "https://meta.fabricmc.net/v2/versions/installer") | Where-Object stable | Select-Object -First 1).version
    $url = "https://meta.fabricmc.net/v2/versions/loader/$MinecraftVersion/$LoaderVersion/$installer/server/jar"
    New-Item -ItemType Directory -Path $installCacheDirectory -Force | Out-Null
    $cachedLauncher = Join-Path $installCacheDirectory "fabric-$MinecraftVersion-$LoaderVersion-launch.jar"
    if (-not (Test-Path -LiteralPath $cachedLauncher)) {
        Invoke-WebRequest $url -OutFile $cachedLauncher
    }
    Copy-Item -LiteralPath $cachedLauncher -Destination (Join-Path $Folder "fabric-server-launch.jar") -Force
    return @{ Jar = "fabric-server-launch.jar"; LoaderVersion = $LoaderVersion }
}

function New-ServerProfile {
    Write-Host ""
    $name = (Read-Host "Profile name").Trim()
    if (-not $name) { return }
    $id = ($name.ToLowerInvariant() -replace '[^a-z0-9]+','-').Trim('-')
    $folderDefault = Join-Path $defaultProfilesDirectory $id
    $folder = (Read-Host "Folder [$folderDefault]").Trim()
    if (-not $folder) { $folder = $folderDefault }
    $minecraftVersion = (Read-Host "Minecraft version (example: 26.2 or 1.12.2)").Trim()
    Assert-MinecraftVersion $minecraftVersion
    $loader = (Read-Host "Loader: vanilla, fabric, or forge").Trim().ToLowerInvariant()
    if ($loader -notin @('vanilla','fabric','forge')) { Write-Host "Unsupported loader." -ForegroundColor Red; return }
    $loaderVersion = ""
    if ($loader -eq 'fabric') {
        $loaderVersion = (Read-Host "Fabric loader version (blank selects latest stable)").Trim()
    } elseif ($loader -eq 'forge') {
        $loaderVersion = (Read-Host "Forge version (example: 14.23.5.2860)").Trim()
    }
    $requiredJava = Get-RequiredJavaMajor $minecraftVersion
    $javaPath = Resolve-JavaPath $requiredJava
    Write-Host "Using Java $requiredJava at $javaPath" -ForegroundColor Green
    $minimumRam = (Read-Host "Minimum RAM [2G]").Trim(); if (-not $minimumRam) { $minimumRam = '2G' }
    $maximumRam = (Read-Host "Maximum RAM [6G]").Trim(); if (-not $maximumRam) { $maximumRam = '6G' }
    if (-not (Test-RamValue $minimumRam) -or -not (Test-RamValue $maximumRam)) { Write-Host "RAM must look like 2G or 4096M." -ForegroundColor Red; return }

    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $folder "mods"),(Join-Path $folder "config"),(Join-Path $folder "backups") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $folder "eula.txt") -Value "eula=true"

    if ($loader -eq 'vanilla') {
        $launchJar = Install-VanillaServer $folder $minecraftVersion
    } elseif ($loader -eq 'fabric') {
        $result = Install-FabricServer $folder $minecraftVersion $loaderVersion
        $launchJar = $result.Jar; $loaderVersion = $result.LoaderVersion
    } else {
        if (-not $loaderVersion) { throw "Forge requires an exact Forge version." }
        $installerName = "forge-$minecraftVersion-$loaderVersion-installer.jar"
        $installerPath = Join-Path $folder $installerName
        New-Item -ItemType Directory -Path $installCacheDirectory -Force | Out-Null
        $cachedInstaller = Join-Path $installCacheDirectory $installerName
        if (-not (Test-Path -LiteralPath $cachedInstaller)) {
            Invoke-WebRequest "https://maven.minecraftforge.net/net/minecraftforge/forge/$minecraftVersion-$loaderVersion/$installerName" -OutFile $cachedInstaller
        }
        Copy-Item -LiteralPath $cachedInstaller -Destination $installerPath -Force
        & $javaPath -jar $installerPath --installServer $folder
        if ($LASTEXITCODE -ne 0) { throw "Forge installer failed." }
        $launchJar = (Get-ChildItem $folder -Filter "forge-$minecraftVersion-$loaderVersion*.jar" | Where-Object Name -NotLike '*installer*' | Select-Object -First 1).Name
        if (-not $launchJar) { throw "Forge launcher jar was not found after installation." }
    }

    if ($loader -in @('vanilla','fabric')) {
        Write-Host "Installing and verifying Minecraft $minecraftVersion..." -ForegroundColor Cyan
        & $javaPath '-Xms512M' '-Xmx1G' '-jar' (Join-Path $folder $launchJar) '--initSettings' 'nogui'
        if ($LASTEXITCODE -ne 0) { throw "Minecraft $minecraftVersion installation verification failed with exit code $LASTEXITCODE." }
    }

    $profile = [ordered]@{ id=$id; name=$name; minecraftVersion=$minecraftVersion; loader=$loader; loaderVersion=$loaderVersion; javaPath=$javaPath; minimumRam=$minimumRam; maximumRam=$maximumRam; launchJar=$launchJar; jvmArguments=@(); serverArguments=@('nogui'); port=25565 }
    $profile | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $folder "profile.json") -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $folder "server.properties") -Value @('server-port=25565','server-ip=','online-mode=true','gamemode=survival','difficulty=normal','hardcore=false','white-list=false','motd=' + $name)

    $registry = Read-Registry
    $registry.profiles += [pscustomobject]@{ id=$id; name=$name; path=[System.IO.Path]::GetFullPath($folder); minecraftVersion=$minecraftVersion; loader=$loader; loaderVersion=$loaderVersion; javaPath=$javaPath; minimumRam=$minimumRam; maximumRam=$maximumRam; launchJar=$launchJar; jvmArguments=@(); serverArguments=@('nogui'); port=25565; runner='generic' }
    Save-Registry $registry
    Write-Host "Profile created and Minecraft $minecraftVersion ($loader) is installed." -ForegroundColor Green
    Write-Host "Add server-compatible mods to $folder\mods" -ForegroundColor Green
}

function Edit-ServerProfile {
    $profile = Select-Profile "Profile to edit"
    if (-not $profile) { return }
    $min = (Read-Host "Minimum RAM [$($profile.minimumRam)]").Trim(); if ($min) { if (-not (Test-RamValue $min)) { throw "Invalid RAM." }; $profile.minimumRam = $min }
    $max = (Read-Host "Maximum RAM [$($profile.maximumRam)]").Trim(); if ($max) { if (-not (Test-RamValue $max)) { throw "Invalid RAM." }; $profile.maximumRam = $max }
    $java = (Read-Host "Java path [$($profile.javaPath)]").Trim(); if ($java) { if (-not (Test-Path $java)) { throw "Java was not found." }; $profile.javaPath = $java }
    $registry = Read-Registry
    for ($i=0; $i -lt $registry.profiles.Count; $i++) { if ($registry.profiles[$i].id -eq $profile.id) { $registry.profiles[$i] = $profile } }
    Save-Registry $registry
    if ($profile.runner -eq 'generic') { $profile | Select-Object id,name,minecraftVersion,loader,loaderVersion,javaPath,minimumRam,maximumRam,launchJar,jvmArguments,serverArguments,port | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $profile.path 'profile.json') -Encoding UTF8 }
    Write-Host "Profile updated." -ForegroundColor Green
}

function Show-Status {
    $listener = Get-ListeningServer
    Write-Host ""
    Get-Profiles | Format-Table name,minecraftVersion,loader,loaderVersion,minimumRam,maximumRam,path -AutoSize
    if ($listener) { Write-Host "Active Minecraft PID: $($listener.OwningProcess) on port 25565" -ForegroundColor Green } else { Write-Host "No active Minecraft server." -ForegroundColor Yellow }
    & $playitExecutable status
}

while ($true) {
    Write-Host ""
    Write-Host "Minecraft Server Profile Manager" -ForegroundColor Cyan
    Write-Host "[1] Start profile"
    Write-Host "[2] Stop active profile"
    Write-Host "[3] Run server command"
    Write-Host "[4] Create profile"
    Write-Host "[5] Edit RAM / Java"
    Write-Host "[6] Status"
    Write-Host "[7] Exit"
    try {
        switch (Read-Host "Choice") {
            '1' { Start-SelectedProfile }
            '2' { Stop-ActiveProfile }
            '3' { Send-ServerCommand }
            '4' { New-ServerProfile }
            '5' { Edit-ServerProfile }
            '6' { Show-Status }
            '7' { exit 0 }
            default { Write-Host "Invalid choice." -ForegroundColor Yellow }
        }
    } catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
    }
}
