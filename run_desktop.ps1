# Run the desktop UI. Usage: right-click -> Run with PowerShell, or from a
# terminal in the repo root: .\run_desktop.ps1
if (-Not (Test-Path ".venv")) {
    python -m venv .venv
}
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-desktop.txt
python -m desktop_app.main