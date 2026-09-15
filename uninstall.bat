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

rem Uninstall: stops the background service, deletes our scheduled tasks and the
rem autostart entry. Reports and credentials are KEPT unless you answer y
rem (or pass --purge). The program folder itself is never deleted -- a running
rem script cannot delete its own folder.

%PYBIN% bootstrap.py uninstall
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%

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
echo    Uninstall by hand instead:
echo      1. Stop the service:  double-click  stop.bat
echo         ^(if that fails, open Task Manager and end pythonw.exe / python.exe^)
echo      2. Delete the scheduled tasks:
echo         open "Task Scheduler" and remove anything named "CBG..."
echo         ^(there may be two: a daily one and a "login" one^)
echo      3. Delete the autostart entry:
echo         Task Manager -^> Startup tab -^> disable CBG
echo      4. Delete this folder.
echo   ============================================================
echo.
pause
exit /b 9009
