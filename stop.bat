@echo off
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem Find a WORKING python. Try "python" first, then the "py" launcher -- plenty of
rem Windows installs have py.exe but nothing on PATH (the "Add python.exe to PATH"
rem box was not ticked during setup).
rem
rem If neither works we MUST stop and say so: every Chinese message is printed BY
rem Python, so without Python there is no Chinese to print. Hence :nopython.
rem ---------------------------------------------------------------------------
set "PYBIN="
rem ---------------------------------------------------------------------------
rem Prefer the interpreter recorded at install time (.secrets\python.txt).
rem bootstrap.py writes it WITH quotes, so %PYBIN% can be used unquoted below
rem even when Python lives in "C:\Program Files\...". It sits in .secrets,
rem which self-update never overwrites -- so the record survives upgrades.
rem When a machine has more than one Python this is what stops "deps were
rem installed into A but this script launched B".
rem No record file yet (first install) -> fall through to probing PATH.
rem ---------------------------------------------------------------------------
if exist ".secrets\python.txt" set /p PYBIN=<".secrets\python.txt"
if not defined PYBIN goto :probepython
%PYBIN% -c "import sys" >nul 2>&1
if not errorlevel 1 goto :havepython
set "PYBIN="

:probepython
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYBIN=python"
if not defined PYBIN (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYBIN=py -3"
)

:havepython
if not defined PYBIN goto :nopython

rem PURE ASCII on purpose -- see the note in bootstrap.py.

%PYBIN% bootstrap.py stop
echo.
echo   Note: this only stops the service. Autostart registration is untouched.
echo         To turn autostart off: console - Settings - Background service.
echo.
pause
exit /b %ERRORLEVEL%

:nopython
echo.
echo   ============================================================
echo    NO WORKING PYTHON FOUND -- this script cannot continue.
echo.
echo    Tried:
echo      python -c "import sys"      ^(failed^)
echo      py -3  -c "import sys"      ^(failed^)
echo.
echo    What "where python" found:
for /f "delims=" %%i in ('where python 2^>nul') do echo        %%i
for /f "delims=" %%i in ('where py 2^>nul') do echo        %%i
echo    ^(nothing listed above = python is not on PATH at all^)
echo.
echo    COMMON CAUSE: the Microsoft Store "python" is a STUB, not Python.
echo    If the Store window opened, that was that stub -- just close it.
echo.
echo    FIX:
echo      1. Python was not found. Get it from
echo           https://www.python.org/downloads/
echo         On Windows 7 you MUST use 3.8.10 -- 3.9 and newer will not
echo         run there at all (missing api-ms-win-core-path DLL):
echo           https://www.python.org/downloads/release/python-3810/
echo      2. During setup TICK the box  "Add python.exe to PATH"
echo      3. Close this window and run this file again
echo   ============================================================
echo.
pause
exit /b 9009
