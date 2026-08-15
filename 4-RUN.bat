@echo off
REM ===========================================================================
REM  4-RUN.bat - Interactive control. Type commands, the phone responds.
REM
REM  The link stays open for the whole session, so each press costs ~2 ms
REM  instead of re-opening the port every time.
REM ===========================================================================

setlocal
cd /d "%~dp0"

echo ============================================================
echo   INTERACTIVE CONTROLLER
echo ============================================================
echo.
echo   Type commands at the  pad^>  prompt. Examples:
echo.
echo     a                  press A
echo     b                  press B
echo     down*3             press D-pad down 3 times
echo     down*2 right a     several things in one line
echo     hold guide 2       hold the Guide button for 2 seconds
echo     stick left right   push the left stick right
echo     trigger rt 255     pull the right trigger fully
echo     macro nav_test     run a macro
echo     state              ask the board what it is holding
echo     reset              release everything
echo     q                  quit
echo.
echo   Full control list:  python host\pad_link.py --list
echo.

python "host\pad_link.py" --interactive

echo.
pause
endlocal
