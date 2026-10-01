@echo off
rem HZ Bot - uruchomienie panelu (Windows). Przy pierwszym starcie instaluje wszystko automatycznie.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Pierwsze uruchomienie - instaluje zaleznosci, to potrwa chwile...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Nie znaleziono Pythona. Zainstaluj go z https://www.python.org/downloads/ ^(zaznacz "Add to PATH"^).
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e ".[capture]" || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m playwright install chromium
)
".venv\Scripts\python.exe" -m hzbot %*
pause
