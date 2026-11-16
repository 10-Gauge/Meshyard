# Builds dist\SC Asset Browser.exe: one file, no console, no web browser needed.
#
#   1. the engine (Python: game indexing, model export, print conversion) as
#      build\engine\sc-engine.exe, self-tested
#   2. the WinUI 3 desktop app (desktop\), published self-contained as a single exe
#      with the engine embedded
#   3. the external tools (StarBreaker + its Blender add-on, cgf-converter) zipped by
#      packaging/bundle_tools.py and appended to the exe; the app unpacks them to
#      %LOCALAPPDATA%\sc-sandbox\tools on first run. -WithBlender also puts a portable
#      Blender inside (about 400 MB more); without it the app uses the installed Blender
#      and offers the download when there is none. -NoTools skips the bundle entirely
#      (a small exe that downloads StarBreaker/cgf-converter itself instead).
#
# Run from the repo root after install.ps1:  powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
# Needs the .NET 10 SDK (or newer) for step 2. CI passes -Python python and -Name SC-Asset-Browser.
param([string]$Python = "", [string]$Name = "SC Asset Browser", [switch]$NoTools, [switch]$WithBlender, [string]$BlenderVersion = "5.2.2")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = if ($Python) { $Python } else { Join-Path $root ".venv\Scripts\python.exe" }
if (-not $Python -and -not (Test-Path $py)) { throw "Run install.ps1 first (no .venv found)." }
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw "Install the .NET SDK (10 or newer) from https://dot.net first." }

Write-Host "== engine"
& $py -m pip install --quiet -e . pyinstaller
if ($LASTEXITCODE) { throw "pip install failed" }
& $py -m PyInstaller --noconfirm --clean --onefile --console `
    --name "sc-engine" `
    --distpath "$root\build\engine" --workpath "$root\build\pyinstaller" --specpath "$root\build" `
    --add-data "$root\scsandbox\web;scsandbox\web" `
    --add-data "$root\scsandbox\blender;scsandbox\blender" `
    --collect-data trimesh `
    --collect-submodules skimage.measure `
    --collect-submodules skimage.morphology `
    --collect-all collada `
    "$root\packaging\sc_engine.py"
if ($LASTEXITCODE) { throw "PyInstaller failed" }
$engine = "$root\build\engine\sc-engine.exe"
& $engine --self-test
if ($LASTEXITCODE) { throw "The engine self-test failed (see %LOCALAPPDATA%\sc-sandbox\self-test.log)" }

Write-Host "== desktop app"
$pub = "$root\build\desktop"
dotnet publish "$root\desktop\SCAssetBrowser.csproj" -c Release -o $pub -p:EngineExe="$engine"
if ($LASTEXITCODE) { throw "dotnet publish failed" }
New-Item -ItemType Directory -Force "$root\dist" | Out-Null
$out = "$root\dist\$Name.exe"
Copy-Item "$pub\SCAssetBrowser.exe" $out -Force

if (-not $NoTools) {
    Write-Host "== bundled tools"
    $bundle = if ($WithBlender) { Join-Path $root "build\sc-tools-blender-$BlenderVersion.zip" } else { Join-Path $root "build\sc-tools.zip" }
    if (-not (Test-Path $bundle)) {
        $bundleArgs = @($bundle)
        if ($WithBlender) { $bundleArgs += @("--with-blender", "--blender-version", $BlenderVersion) }
        & $py (Join-Path $root "packaging\bundle_tools.py") @bundleArgs
        if ($LASTEXITCODE) { throw "bundling the tools failed" }
    }
    # exe | zip | zip length (int64) | "SCTOOLS1"  (read by desktop\Services\BundledTools.cs)
    $len = (Get-Item $bundle).Length
    $fs = [IO.File]::Open($out, [IO.FileMode]::Append)
    try {
        $src = [IO.File]::OpenRead($bundle)
        try { $src.CopyTo($fs) } finally { $src.Dispose() }
        $fs.Write([BitConverter]::GetBytes([long]$len), 0, 8)
        $magic = [Text.Encoding]::ASCII.GetBytes("SCTOOLS1")
        $fs.Write($magic, 0, 8)
    } finally { $fs.Dispose() }
}
Write-Host ("Built: $out ({0:N0} MB)" -f ((Get-Item $out).Length / 1MB))
