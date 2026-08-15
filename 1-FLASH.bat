@echo off
REM ===========================================================================
REM  1-FLASH.bat - Double-click to flash the Leonardo. That is the whole job.
REM
REM  Handles all of this for you:
REM    * installs the arduino:avr core if missing
REM    * installs the CORRECT Joystick library if missing (see note below)
REM    * compiles the sketch
REM    * waits for you to tap RESET, then flashes the instant the board appears
REM
REM  THE LIBRARY NOTE: `arduino-cli lib install "Joystick"` installs the WRONG
REM  library - a different author's, for READING physical thumbsticks. The
REM  HID-emulation one we need (MHeironimus) is not in the Arduino index at all.
REM  flash.py detects this and fixes it automatically.
REM
REM  Connect the Leonardo to THIS PC by USB before running.
REM ===========================================================================

setlocal
cd /d "%~dp0"

echo.
echo  Before starting: the Leonardo must be connected to THIS PC by USB.
echo  (Not the phone - we are flashing it right now.)
echo.
pause

python "host\flash.py"
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
    echo  Flashed. Next: 2-CHECK.bat once the phone and FT232RL are connected.
) else (
    echo  Flash did not complete - read the messages above.
    echo  Most often: just run this again and tap RESET when prompted.
)
echo.

pause
endlocal
exit /b %RC%
