@echo off
REM ===========================================================================
REM  3-TEST.bat - Exercise every control so you can watch it on the phone.
REM
REM  Runs two things:
REM    nav_test      - an obvious D-pad pattern (right right left left down up)
REM    hid_selftest  - all 18 controls once each
REM
REM  This is also what clears xCloud's "connect a controller" prompt: it takes
REM  one input for the app to notice a pad exists.
REM ===========================================================================

setlocal
cd /d "%~dp0"

echo ============================================================
echo   CONTROL TEST - watch your phone
echo ============================================================
echo.
echo   For the clearest result, have ONE of these open on the phone:
echo     * xCloud (Xbox Game Pass) - the menu selection should move
echo     * a "Gamepad Tester" app  - buttons light up individually
echo.
pause

echo.
echo ------------------------------------------------------------
echo   nav_test - visible D-pad pattern
echo ------------------------------------------------------------
python "host\pad_link.py" macro nav_test

echo.
echo ------------------------------------------------------------
echo   hid_selftest - every control once
echo ------------------------------------------------------------
python "host\pad_link.py" macro hid_selftest

echo.
echo ============================================================
echo   Did the phone react?
echo ============================================================
echo   YES -^> everything works. Use 4-RUN.bat to drive it.
echo.
echo   NO, but 2-CHECK.bat passed -^> the board is receiving commands
echo   but the phone is not accepting the HID pad. Check:
echo     * the Leonardo's ON LED is lit (phone must power the board)
echo     * the OTG adapter is at the PHONE end, never the board end
echo     * the cable carries DATA, not just power
echo     * replug the phone cable with the screen on and unlocked
echo.

pause
endlocal
