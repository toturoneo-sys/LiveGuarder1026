@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  LiveGuarder1026 - Windows EXE Build
echo ============================================
echo.

py -m pip install --upgrade pip
if errorlevel 1 goto :error

py -m pip install -r requirements.txt
if errorlevel 1 goto :error

py -m pip install pyinstaller
if errorlevel 1 goto :error

echo.
echo [1/2] Python syntax check
py -m py_compile LiveGuarder1026.py
if errorlevel 1 goto :error

echo.
echo [2/2] Building EXE
pyinstaller --clean --noconfirm LiveGuarder1026.spec
if errorlevel 1 goto :error

echo.
echo ============================================
echo  Build complete
echo ============================================
echo.
echo EXE:
echo dist\LiveGuarder1026.exe
echo.
pause
exit /b 0

:error
echo.
echo ============================================
echo  Build failed
echo ============================================
echo.
pause
exit /b 1
