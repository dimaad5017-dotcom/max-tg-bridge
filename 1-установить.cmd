@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Установка моста MAX - Telegram

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python не найден.
  echo Скачай его с https://www.python.org/downloads/ и при установке
  echo обязательно поставь галочку "Add python.exe to PATH".
  echo Потом запусти этот файл ещё раз.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Создаю отдельное окружение для моста...
  python -m venv .venv
  if errorlevel 1 goto fail
)

echo Ставлю библиотеки, это займёт минуту...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

if not exist ".env" copy ".env.example" ".env" >nul

echo.
echo Готово. Дальше по порядку: 2-настроить.cmd, 3-войти-в-MAX.cmd, 4-запустить-мост.cmd
echo.
pause
exit /b 0

:fail
echo.
echo Установка сорвалась. Скопируй последние строки выше — по ним понятно, что случилось.
echo Разбор частых ошибок есть в README, раздел «Что могло пойти не так».
echo.
pause
exit /b 1
