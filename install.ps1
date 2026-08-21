$ErrorActionPreference = "Stop"
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .
Write-Host "Installed. Run: .\\.venv\\Scripts\\awb --help"
