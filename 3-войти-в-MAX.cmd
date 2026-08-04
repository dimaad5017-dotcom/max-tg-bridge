@echo off
chcp 866 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запусти 1-установить.cmd - без него мост не установлен.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m bridge.login
echo.
pause
