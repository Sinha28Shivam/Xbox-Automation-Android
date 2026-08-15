@echo off
REM ==========================================================================
REM  5-AGENTIC.bat - run the multi-agent xCloud tester.
REM
REM  Follows 1-FLASH / 2-CHECK / 3-TEST / 4-RUN. Those four are manual: you
REM  press buttons and watch the phone yourself. This one is different - you
REM  describe WHAT to verify, and the agents decide how, watch the screen, and
REM  write a report that can say "the input did nothing" - which a firmware OK
REM  can never say.
REM
REM  Usage:
REM    5-AGENTIC.bat                       :: the smoke suite
REM    5-AGENTIC.bat --list                :: every suite and case
REM    5-AGENTIC.bat --check               :: probe the rig, send nothing
REM    5-AGENTIC.bat --capabilities        :: what the agents may do
REM    5-AGENTIC.bat --suite controller    :: a named suite
REM    5-AGENTIC.bat --case pwa_loads      :: one case
REM    5-AGENTIC.bat "check the A button"  :: describe it yourself
REM    5-AGENTIC.bat --dry-run --suite smoke
REM
REM  The pushd is the point of this file: main.py must run with the package
REM  folder as the working directory, and double-clicking a .bat starts in
REM  whatever directory Explorer felt like.
REM ==========================================================================

setlocal
pushd "%~dp0Android-Agentic-Xcloud-Testing"

if "%~1"=="" (
    echo No arguments given - running the SMOKE suite.
    echo.
    echo   5-AGENTIC.bat --list     to see every suite and case
    echo   5-AGENTIC.bat --check    to confirm the rig before testing
    echo.
    python main.py --suite smoke
) else (
    python main.py %*
)

set EXITCODE=%ERRORLEVEL%
echo.
echo ----------------------------------------------------------------------
REM The exit code IS the verdict, for CI. Note that `inconclusive` is
REM deliberately NOT 0: a run that proved nothing must not turn a build green.
if %EXITCODE%==0 echo VERDICT: PASS
if %EXITCODE%==1 echo VERDICT: FAIL          - see the report's root-cause section
if %EXITCODE%==2 echo VERDICT: BLOCKED       - the rig was not ready; nothing ran
if %EXITCODE%==3 echo VERDICT: INCONCLUSIVE  - it ran, but proved nothing either way
if %EXITCODE%==4 echo VERDICT: ERROR         - the harness itself failed
echo Reports: Android-Agentic-Xcloud-Testing\reports\
echo ----------------------------------------------------------------------

popd
endlocal & exit /b %EXITCODE%
