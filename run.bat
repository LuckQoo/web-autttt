@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { . .\\.venv\\Scripts\\Activate.ps1; pythonw app.py }"
