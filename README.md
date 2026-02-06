python -m venv .venv
.venv\Scripts\Activate.ps1
pip install PySide6 playwright
python -m playwright install chromium
python app.py
