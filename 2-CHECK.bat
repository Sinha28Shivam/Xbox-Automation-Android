@echo off
REM ===========================================================================
REM  2-CHECK.bat - Is everything connected and talking?
REM
REM  Safe to run any time: sends no input and changes nothing.
REM
REM  Expects the full working chain:
REM      PC --USB--> [FT232RL] --UART--> [Leonardo] --USB+OTG--> [PHONE]
REM ===========================================================================

setlocal
cd /d "%~dp0"

echo ============================================================
echo   STEP 1 - serial ports
echo ============================================================
python "host\pad_link.py" ports
echo.
echo   Expect the FT232RL:  COM8  0403:6001
echo.
echo   NOTE: the Leonardo should NOT be listed here. It is plugged
echo   into the phone, so the PC cannot see it - that is correct.
echo   We reach it over the UART via the FT232RL instead.
echo.

echo ============================================================
echo   STEP 2 - does the firmware answer?
echo ============================================================
python "host\pad_link.py" --check
echo.

echo ============================================================
echo   READING THE RESULT
echo ============================================================
echo   Expect:  firmware : xcloudpad-usb-1.0
echo.
echo   "no PONG from the board" means the UART link is broken:
echo     * check TX-^>D0, RX-^>D1, GND-^>GND (TX and RX must CROSS)
echo     * check the FT232RL voltage jumper is on 5V, not 3.3V
echo     * close the Arduino Serial Monitor if it is open
echo.
echo   Also confirm the Leonardo's ON LED is lit. That proves the
echo   phone entered USB host mode and is powering the board. If it
echo   is dark, the phone is not in host mode and no input can work.
echo.

pause
endlocal
