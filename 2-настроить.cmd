@echo off
chcp 866 >nul
cd /d "%~dp0"
if not exist ".env" copy ".env.example" ".env" >nul
notepad .env
