# Builds dist\GUIConnect.exe (all Python deps bundled) then dist\GUIConnect-Setup.exe.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv\Scripts\python.exe)) { python -m venv .venv }
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller==6.22.3
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --hide-console hide-early --icon $PWD\kepclone\kepclone.ico --add-data "$PWD\kepclone\kepclone.ico;kepclone" --name GUIConnect --collect-all asyncua --workpath $env:TEMP\kepclone-build --specpath $env:TEMP\kepclone-build kepclone_exe.py

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    winget install --id JRSoftware.InnoSetup -e --scope user --accept-package-agreements --accept-source-agreements
    $iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
}
& $iscc installer\GUIConnect.iss
Write-Host "Installer: $PWD\dist\GUIConnect-Setup.exe"
