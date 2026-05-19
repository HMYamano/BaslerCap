@echo off
REM ---------------------------------------------------------------
REM  Basler Recorder - one-time environment setup.
REM  Creates a conda environment under .\env and installs deps.
REM  Run this once. After that, use run.bat to launch the app.
REM ---------------------------------------------------------------

setlocal
cd /d "%~dp0"

set CONDA_BASE=%USERPROFILE%\miniconda3
if not exist "%CONDA_BASE%\Scripts\activate.bat" (
    set CONDA_BASE=%USERPROFILE%\anaconda3
)
if not exist "%CONDA_BASE%\Scripts\activate.bat" (
    echo [ERROR] Could not find miniconda3 or anaconda3 under %USERPROFILE%.
    echo Install Miniconda from https://docs.conda.io/en/latest/miniconda.html
    exit /b 1
)

set ENV_DIR=%~dp0env
set PY_VER=3.11

if exist "%ENV_DIR%\python.exe" (
    echo [setup] Environment already exists at %ENV_DIR%
) else (
    echo [setup] Creating conda env at %ENV_DIR% with Python %PY_VER% ...
    call "%CONDA_BASE%\Scripts\activate.bat" "%CONDA_BASE%"
    call conda create -y -p "%ENV_DIR%" python=%PY_VER%
    if errorlevel 1 (
        echo [ERROR] conda create failed.
        exit /b 1
    )
)

echo [setup] Activating env and installing requirements ...
call "%CONDA_BASE%\Scripts\activate.bat" "%ENV_DIR%"
python -m pip install --upgrade pip
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] pip install failed.
    exit /b 1
)

echo.
echo [setup] Done. Launch the app with run.bat
endlocal
