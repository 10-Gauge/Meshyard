# One-time setup on Windows. Run from the repo folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Re-run it after `git pull` to pick up toolkit updates.
# Needs Python 3.10+ (winget install Python.Python.3.12).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-Command py -ErrorAction SilentlyContinue) { $base = "py"; $baseArgs = @("-3") }
else { $base = "python"; $baseArgs = @() }
# Windows ships a "python" placeholder that only opens the Store, so test that Python really runs.
$ok = $false
try { & $base @baseArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null; $ok = ($LASTEXITCODE -eq 0) } catch {}
if (-not $ok) {
    Write-Host "Python 3.10 or newer is needed. Install it with:" -ForegroundColor Yellow
    Write-Host "    winget install Python.Python.3.12"
    Write-Host "then close and reopen PowerShell and run this script again."
    exit 1
}

if (-not (Test-Path .venv)) { & $base @baseArgs -m venv .venv }
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -e .

Write-Host "`nDownloading StarBreaker and cgf-converter..."
& $venvPy -m scsandbox tools update

Write-Host "`nDetected installs:"
& $venvPy -m scsandbox installs

# Double-click launcher for the browser
$launcher = Join-Path $PSScriptRoot "Meshyard.cmd"
"@echo off`r`n`"$venvPy`" -m scsandbox serve`r`n" | Set-Content -Encoding ASCII $launcher
Write-Host "`nDone. Start the browser with: `"$launcher`""
Write-Host "Optional: .venv\Scripts\meshyard blender install   (adds the sandbox panel to Blender)"
Write-Host "Optional: .venv\Scripts\meshyard schedule          (re-index LIVE and PTU daily)"
