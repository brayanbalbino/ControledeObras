@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Procura um Python estável (3.13 a 3.10); se não achar, usa o "python" do PATH
set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY set "PY=python"

if not exist ".venv" (
  echo Criando ambiente virtual...
  %PY% -m venv .venv || goto erro
)
call .venv\Scripts\activate.bat
python -m pip install -q --disable-pip-version-check dash plotly openpyxl || goto erro

python controle_obras.py
pause
exit /b

:erro
echo.
echo Algo deu errado. Instale o Python 3.12 em python.org e rode este arquivo de novo.
pause
