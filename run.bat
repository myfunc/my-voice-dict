@echo off
setlocal

REM Change to the directory of this script
pushd "%~dp0"

REM Create venv if missing (prefer py launcher, fallback to python)
if not exist ".venv\Scripts\python.exe" (
	where py >nul 2>&1 && py -3 -m venv .venv || python -m venv .venv
)

REM Upgrade pip tooling
call ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel

REM Install requirements if present
if exist requirements.txt (
	call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

REM Ensure Windows-friendly VAD wheel (avoid building webrtcvad from source)
call ".venv\Scripts\python.exe" -m pip install --upgrade webrtcvad-wheels >nul 2>&1
call ".venv\Scripts\python.exe" -c "import webrtcvad" 1>nul 2>nul
if errorlevel 1 (
	REM If import still fails, try reinstall
	call ".venv\Scripts\python.exe" -m pip install --force-reinstall webrtcvad-wheels
)

REM Start the app
echo Starting VoiceDict...
call ".venv\Scripts\python.exe" voice_dict.py

popd
endlocal
