@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0Нормальный транскрайб.exe" (
  start "" "%~dp0Нормальный транскрайб.exe"
) else if exist "%~dp0Расшифровка.exe" (
  start "" "%~dp0Расшифровка.exe"
) else if exist "%~dp0runtime\python\pythonw.exe" (
  start "" "%~dp0runtime\python\pythonw.exe" "%~dp0app_gui.py"
) else (
  echo Нужна вся папка «Нормальный транскрайб» с runtime.
  pause
)
