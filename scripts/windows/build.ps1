[CmdletBinding()]
param(
    [switch]$SkipTests,
    [string]$InnoCompiler
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$workDir = Join-Path $projectRoot 'work\build'
$dependencyDir = Join-Path $workDir 'hidapi-0.15.0'
$zipPath = Join-Path $workDir 'hidapi-win.zip'
$dllPath = Join-Path $dependencyDir 'x64\hidapi.dll'
$venvDir = Join-Path $workDir '.venv'
$pythonExe = Join-Path $venvDir 'Scripts\python.exe'
$expectedZipHash = 'D18C43EC9506A2F6D7FAA9C7E0A342C4B64FBAE521B71B5D4AC0777FD24DDA93'
$downloadUrl = 'https://github.com/libusb/hidapi/releases/download/hidapi-0.15.0/hidapi-win.zip'

New-Item -ItemType Directory -Force -Path $workDir | Out-Null
if (-not (Test-Path -LiteralPath $zipPath)) {
    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $zipPath
}
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
if ($actualHash -ne $expectedZipHash) {
    throw "hidapi archive hash mismatch: expected $expectedZipHash, got $actualHash"
}
if (-not (Test-Path -LiteralPath $dllPath)) {
    Expand-Archive -LiteralPath $zipPath -DestinationPath $dependencyDir -Force
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv $venvDir
}
& $pythonExe -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'build dependency installation failed' }
& $pythonExe -m pip install --disable-pip-version-check --no-deps --editable $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'LogiPair installation failed' }

Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        & $pythonExe -m ruff check src\logipair tests scripts\generate_icon.py scripts\verify_icon.py
        if ($LASTEXITCODE -ne 0) { throw 'ruff failed' }
        & $pythonExe -m pytest
        if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }
    }
    $iconPath = Join-Path $projectRoot 'assets\logipair.ico'
    if (-not (Test-Path -LiteralPath $iconPath)) { throw "application icon not found: $iconPath" }
    & $pythonExe -m PyInstaller --noconfirm --clean --onefile --name LogiPair --paths src --icon $iconPath --add-binary "$dllPath;." src\logipair\__main__.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    & $pythonExe (Join-Path $projectRoot 'scripts\verify_icon.py') (Join-Path $projectRoot 'dist\LogiPair.exe')
    if ($LASTEXITCODE -ne 0) { throw 'icon resource verification failed' }
    & (Join-Path $projectRoot 'dist\LogiPair.exe') --version
    if ($LASTEXITCODE -ne 0) { throw 'binary smoke test failed' }

    if (-not $InnoCompiler) {
        $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($command) {
            $InnoCompiler = $command.Source
        } else {
            $candidate = Join-Path $env:USERPROFILE 'Tools\Inno Setup 6\ISCC.exe'
            if (Test-Path -LiteralPath $candidate) { $InnoCompiler = $candidate }
        }
    }
    if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler)) {
        throw 'Inno Setup 6 ISCC.exe was not found'
    }
    & $InnoCompiler (Join-Path $projectRoot 'installer\LogiPair.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }

    $exe = Join-Path $projectRoot 'dist\LogiPair.exe'
    $installer = Join-Path $projectRoot 'dist\LogiPair-Setup-1.0.0.exe'
    $manifest = [ordered]@{
        version = '1.0.0'
        python = (& $pythonExe --version 2>&1 | Out-String).Trim()
        hidapi = '0.15.0'
        executable = [ordered]@{ path = 'LogiPair.exe'; sha256 = (Get-FileHash $exe -Algorithm SHA256).Hash }
        installer = [ordered]@{ path = 'LogiPair-Setup-1.0.0.exe'; sha256 = (Get-FileHash $installer -Algorithm SHA256).Hash }
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $projectRoot 'dist\build-manifest.json') -Encoding utf8
    Write-Host "[PASS] Installer: $installer"
} finally {
    Pop-Location
}
