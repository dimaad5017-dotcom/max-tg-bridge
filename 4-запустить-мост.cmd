@echo off
chcp 866 >nul
cd /d "%~dp0"
title Мост MAX - Telegram (не закрывай, пока нужен)

if not exist ".venv\Scripts\python.exe" (
  echo Сначала запусти 1-установить.cmd - без него мост не установлен.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m bridge.run
echo.
echo Мост выключен. Чтобы включить снова - запусти этот файл ещё раз.
echo.
pause
