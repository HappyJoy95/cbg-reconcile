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
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYBIN=python"
if not defined PYBIN (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYBIN=py -3"
)
if not defined PYBIN goto :nopython

rem PURE ASCII on purpose -- see the note in bootstrap.py.
rem service-start does: already running -> just open the browser;
rem otherwise spawn in background, wait until ready, print the result.

%PYBIN% bootstrap.py service-start
if errorlevel 1 goto :failed

echo.
echo   Press any key to close this window
echo   (the service keeps running in the background)
pause >nul
exit /b 0

:failed
echo.
echo   Start failed. Run install.bat first, or selftest.bat for details.
echo.
pause
exit /b 1

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
echo      1. Install Python 3.14 from   https://www.python.org/downloads/
echo      2. During setup TICK the box  "Add python.exe to PATH"
echo      3. Close this window and run this file again
echo   ============================================================
echo.
pause
exit /b 9009
