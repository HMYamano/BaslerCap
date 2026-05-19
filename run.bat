@echo off
cd /d "%~dp0"

set CONDA_BASE=%USERPROFILE%\miniconda3
if not exist "%CONDA_BASE%\Scripts\activate.bat" (
    set CONDA_BASE=%USERPROFILE%\anaconda3
)

if not exist "%~dp0env\python.exe" (
    echo [ERROR] env not found. Run setup.bat first.
    pause
    exit /b 1
)

call "%CONDA_BASE%\Scripts\activate.bat" "%~dp0env"
python main.py
