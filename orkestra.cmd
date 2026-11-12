@echo off
rem Orkestra baslatici (Windows): uygun Python surumunu (3.11 ve ustu) bulur ve Orkestra'yi calistirir.
rem Kullanim:  .\orkestra.cmd            (terminal)
rem            .\orkestra.cmd --mock     (sahte ajanlarla deneme)
rem            .\orkestra.cmd doctor | hesap | ui | run "hedef"
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
for %%V in (3.14 3.13 3.12 3.11) do (
  py -%%V -c "pass" >nul 2>&1 && (set "ORK_PY=py -%%V" & goto :run)
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && (set "ORK_PY=python" & goto :run)
echo Python 3.11 veya ustu bulunamadi. https://www.python.org/downloads/ adresinden kur ("Add python.exe to PATH" isaretli olsun).
exit /b 1
:run
%ORK_PY% -m orkestra %*
exit /b %errorlevel%
