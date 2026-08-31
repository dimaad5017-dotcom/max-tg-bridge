@echo off
chcp 866 >nul
cd /d "%~dp0"
title Лог моста MAX - Telegram

if not exist "cache\bridge.log" (
  echo Лога пока нет: мост ещё ни разу не запускался.
  echo Запусти 4-запустить-мост.cmd - файл появится.
  echo.
  pause
  exit /b 1
)

start "" notepad "cache\bridge.log"
