@echo off
rem Builds a single-file dist\ModLoader.exe with PyInstaller.
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller || goto :error
python -m PyInstaller --noconfirm --onefile --windowed --name ModLoader ModLoader.pyw || goto :error
echo.
echo Built dist\ModLoader.exe
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
