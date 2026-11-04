@echo off
setlocal
cd /d "%~dp0"
title Building Tableside
echo.
echo  ==============================================
echo    Building the Tableside installer
echo    Takes 5-15 minutes the first time.
echo  ==============================================
echo.

rem ---- 1. Find Python 3.12 / 3.11 / 3.13 (with Tk) --------------------------
set PY=
py -3.12 -c "import tkinter" >nul 2>nul && set PY=py -3.12
if not defined PY py -3.11 -c "import tkinter" >nul 2>nul && set PY=py -3.11
if not defined PY py -3.13 -c "import tkinter" >nul 2>nul && set PY=py -3.13
if defined PY goto have_python

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" goto found_local
echo Python was not found. Installing Python 3.12 (one time)...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" goto found_local
echo.
echo  Could not install Python automatically.
echo  Install Python 3.12 from https://www.python.org/downloads/ and run this again.
pause
exit /b 1

:found_local
set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

:have_python
echo Using %PY%

rem ---- 2. Build environment ---------------------------------------------------
if exist ".build_env\Scripts\python.exe" goto have_env
echo Creating the build environment...
%PY% -m venv .build_env
if not exist ".build_env\Scripts\python.exe" goto fail

:have_env
".build_env\Scripts\python.exe" -m pip install --upgrade pip
".build_env\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

rem ---- 3. Build Tableside.exe -------------------------------------------------
echo.
echo Building Tableside.exe...
".build_env\Scripts\python.exe" -m PyInstaller --noconfirm --clean tableside.spec
if errorlevel 1 goto fail
if not exist "dist\Tableside\Tableside.exe" goto fail

rem ---- 4. Find or install Inno Setup, then build the installer ----------------
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto have_iscc
set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto have_iscc
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto have_iscc
echo Installing Inno Setup (makes the installer, one time)...
winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto have_iscc
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto have_iscc
echo.
echo  Tableside.exe was built in dist\Tableside, but Inno Setup isn't installed,
echo  so the single-file installer wasn't made. Install it from https://jrsoftware.org/isdl.php
echo  and run this again.
explorer "dist\Tableside"
pause
exit /b 1

:have_iscc
"%ISCC%" installer.iss
if errorlevel 1 goto fail

echo.
echo  ==============================================
echo    Done!  dist\TablesideSetup.exe
echo    Share that one file. Double-click to install.
echo  ==============================================
explorer "dist"
pause
exit /b 0

:fail
echo.
echo  The build stopped because of an error - see the messages above.
pause
exit /b 1
