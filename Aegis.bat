@echo off
rem Starts Aegis. The first run sets up its environment; after that it opens straight away and needs no internet.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" if exist ".venv\requirements.installed" fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul && goto speech

echo Setting up Aegis. This needs the internet once and takes a few minutes.
if exist ".venv\Scripts\python.exe" goto install
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was not found. Install Python 3.10 or newer from python.org, then run this again.
  pause
  exit /b 1
)
python -m venv .venv --system-site-packages
if errorlevel 1 (
  echo.
  echo Could not create the Python environment.
  pause
  exit /b 1
)

:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Could not install what Aegis needs. Check your internet connection and run this again.
  pause
  exit /b 1
)
copy /y requirements.txt ".venv\requirements.installed" >nul

:speech
rem Voice needs one model. If this fails, Aegis still opens and offers the download in Settings.
if not exist "%LOCALAPPDATA%\Aegis2\models\speech\" (
  echo Downloading the speech model, about 670 MB. This only happens once.
  ".venv\Scripts\python.exe" -m aegis.speech
)

:launch
if not exist "web\dist\index.html" (
  echo The Aegis interface has not been built. Run "npm install" and "npm run build" in the web folder.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m aegis
exit /b 0
