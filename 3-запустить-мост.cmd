@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Мост MAX - Telegram (не закрывай, пока нужен)

:loop
".venv\Scripts\python.exe" -m bridge.main
echo.
echo Мост остановился. Через 10 секунд подниму заново.
echo Чтобы выключить совсем — закрой это окно крестиком.
timeout /t 10 >nul
goto loop
