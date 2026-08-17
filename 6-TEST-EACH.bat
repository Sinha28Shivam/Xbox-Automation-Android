@echo off
REM ===========================================================================
REM  6-TEST-EACH.bat - test EVERY control one at a time and record what you saw.
REM
REM  HOW THIS DIFFERS FROM 3-TEST.bat
REM  3-TEST.bat fires all 18 controls in a few seconds, then asks "Did the phone
REM  react?" ONCE, about all of them together. If you answer no you learn nothing
REM  about WHICH control failed - and if only one is broken you will probably
REM  answer yes and never find out.
REM
REM  This script asks after EVERY control, so the result is a per-control table
REM  instead of a single yes/no. Use 3-TEST.bat for a fast "is it alive", and
REM  this one when something is wrong and you need to know what.
REM
REM  It takes a few minutes. That is the point: a firmware OK proves only that
REM  the report was queued, so a human has to confirm the phone reacted.
REM ===========================================================================

setlocal
cd /d "%~dp0"

echo ============================================================
echo   PER-CONTROL TEST
echo ============================================================
echo.
echo   Open ONE of these on the phone before continuing:
echo.
echo     1. a "Gamepad Tester" app  - BEST. Every button lights up
echo        individually and sticks/triggers show numbers, so you can
echo        tell lb from rb and see a trigger's analog range.
echo.
echo     2. xCloud - realistic, but a poor instrument here: only the
echo        D-pad and A/B change anything visibly, so x, y, view, ls,
echo        rs and both triggers will look dead even when perfect.
echo.
echo   You will be asked what you SAW after each control.
echo   Enter means "not sure" and is recorded as unproven, never a pass.
echo.

python "host\test-controller.py" --report "controller-test-report.md" %*

set RESULT=%errorlevel%
echo.
echo ============================================================
if "%RESULT%"=="0" echo   ALL CONFIRMED - every tested control reached the phone.
if "%RESULT%"=="1" echo   SILENT FAILURE - something was accepted but did nothing.
if "%RESULT%"=="2" echo   INCONCLUSIVE - nothing could be confirmed either way.
if "%RESULT%"=="3" echo   SEND FAILURE - the board refused. Fix the link first.
echo ============================================================
echo.
echo   A table was written to controller-test-report.md
echo.
if "%RESULT%"=="3" (
  echo   The reports never left the PC, so this is NOT an Android
  echo   problem. Run 2-CHECK.bat and check the UART wiring:
  echo     * FT232RL TX -^> Leonardo RX ^(D0^), RX -^> TX ^(D1^) - they CROSS
  echo     * a COMMON GND between the two boards
  echo     * the FTDI voltage jumper on 5V, not 3.3V
  echo.
)
if "%RESULT%"=="1" (
  echo   Before believing a failure, rule out the instrument:
  echo     * re-test in a Gamepad Tester app, not xCloud
  echo     * `guide` is flagged UNVERIFIED - some Android builds
  echo       swallow the HID Home usage entirely
  echo     * if the D-pad worked and nothing else did, the pad IS
  echo       connected and the fault is per-button
  echo     * if NOTHING reacted, suspect the link: ON LED dark, OTG
  echo       adapter at the wrong end, or a charge-only cable
  echo.
)

echo   Useful variations:
echo     6-TEST-EACH.bat --faces             just A, B, X, Y
echo     6-TEST-EACH.bat --dpad              just the D-pad
echo     6-TEST-EACH.bat --only lt rt        just the triggers
echo     6-TEST-EACH.bat --only a b up down  the four xCloud reacts to
echo     6-TEST-EACH.bat --auto              send all, ask nothing
echo.


pause
endlocal
exit /b %RESULT%
