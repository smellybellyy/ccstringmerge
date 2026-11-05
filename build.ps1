# Builds dist\CCStringMerge.exe (one file, no Python needed) and dist\SHA256SUMS.txt.
# Needs: python -m venv .venv; .venv\Scripts\python -m pip install pyinstaller
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& .\.venv\Scripts\python.exe -I -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw 'tests failed' }
& .\.venv\Scripts\python.exe -m PyInstaller --onefile --console --clean --noconfirm --name CCStringMerge ccstringmerge.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
$h = (Get-FileHash -Algorithm SHA256 dist\CCStringMerge.exe).Hash.ToLower()
"$h  CCStringMerge.exe" | Out-File -Encoding ascii dist\SHA256SUMS.txt
Get-Content dist\SHA256SUMS.txt
