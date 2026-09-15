@echo off
rem ===========================================================================
rem  One-click diagnostic. Writes everything to diagnose-result.txt.
rem
rem  PURE ASCII and NO PYTHON REQUIRED on purpose: if the problem IS python
rem  (missing / Microsoft Store stub), a Python-based tool could not run either.
rem  That is exactly the situation this file exists for.
rem ===========================================================================
setlocal
cd /d "%~dp0"
set OUT=%~dp0diagnose-result.txt

> "%OUT%" echo === CBG diagnose ===
>> "%OUT%" echo date: %DATE% %TIME%
>> "%OUT%" echo dir:  %CD%
>> "%OUT%" echo.

>> "%OUT%" echo --- 1. where python / pythonw ---
where python  >> "%OUT%" 2>&1
where pythonw >> "%OUT%" 2>&1
where py      >> "%OUT%" 2>&1
>> "%OUT%" echo.

>> "%OUT%" echo --- 2. python version ---
python -V >> "%OUT%" 2>&1
echo python -V exit=%ERRORLEVEL% >> "%OUT%"
>> "%OUT%" echo.

>> "%OUT%" echo --- 3. real interpreter path ---
python -c "import sys;print(sys.executable);print(sys.version)" >> "%OUT%" 2>&1
echo exit=%ERRORLEVEL% >> "%OUT%"
>> "%OUT%" echo.

>> "%OUT%" echo --- 4. dependencies ---
python -c "import requests,yaml,openpyxl;print('deps OK')" >> "%OUT%" 2>&1
echo exit=%ERRORLEVEL% >> "%OUT%"
>> "%OUT%" echo.

>> "%OUT%" echo --- 5. files present? ---
for %%F in (run.bat run-now.bat run_check.py bootstrap.py boot.py BUILD.txt) do (
  if exist "%%F" (>> "%OUT%" echo   OK      %%F) else (>> "%OUT%" echo   MISSING %%F)
)
if exist config (>> "%OUT%" echo   OK      config\) else (>> "%OUT%" echo   MISSING config\)
if exist .secrets (>> "%OUT%" echo   OK      .secrets\) else (>> "%OUT%" echo   MISSING .secrets\)
if exist out (>> "%OUT%" echo   OK      out\) else (>> "%OUT%" echo   MISSING out\)
>> "%OUT%" echo.

>> "%OUT%" echo --- 6. run.bat content ---
type run.bat >> "%OUT%" 2>&1
>> "%OUT%" echo.

>> "%OUT%" echo --- 7. out\run.log (whole file) ---
if exist out\run.log (type out\run.log >> "%OUT%" 2>&1) else (>> "%OUT%" echo   (no out\run.log))
>> "%OUT%" echo.

>> "%OUT%" echo --- 8. our scheduled tasks ---
schtasks /query /fo LIST >> "%OUT%" 2>&1
>> "%OUT%" echo.

>> "%OUT%" echo --- 9. live attempt: run the check right now ---
rem This is the important part: it runs SYNCHRONOUSLY so any error is captured.
set CFG=
for %%F in (config\store-*.yaml) do set CFG=%%F
>> "%OUT%" echo config used: %CFG%
python run_check.py -c "%CFG%" check --days-ago 1 >> "%OUT%" 2>&1
echo exit=%ERRORLEVEL% >> "%OUT%"
>> "%OUT%" echo.

>> "%OUT%" echo === done ===

echo.
echo   Result written to:
echo     %OUT%
echo.
echo   Please send that file back.
echo.
pause
exit /b 0
