@echo off
REM ==========================================================================
REM  5-AGENTIC.bat - run the multi-agent xCloud tester.
REM
REM  Follows 1-FLASH / 2-CHECK / 3-TEST / 4-RUN. Those four are manual: you
REM  press buttons and watch the phone. This one is different - you describe
REM  WHAT to verify and the agents decide how, watch the screen, and write a
REM  report that can say "the input did nothing", which a firmware OK cannot.
REM
REM  Usage:
REM    5-AGENTIC.bat                                  :: the default scenario
REM    5-AGENTIC.bat --check                          :: probe the rig only
REM    5-AGENTIC.bat --capabilities                   :: what can this run do?
REM    5-AGENTIC.bat "open xCloud and check the pad is detected"
REM    5-AGENTIC.bat --scenario scenarios\ --all      :: the whole suite
REM    5-AGENTIC.bat --dry-run "..."                  :: plan, touch nothing
REM
REM  The pushd is the point of this file: `python -m agentic` must run with the
REM  package folder as the working directory, and double-clicking a .bat starts
REM  in whatever directory Explorer felt like.
REM ==========================================================================

setlocal
pushd "%~dp0Android-Agentic-Xcloud-Testing"

if "%~1"=="" (
    echo No scenario given - running the default: scenarios\controller_detected.yaml
    echo.
    echo   Tip: 5-AGENTIC.bat --check          confirms the rig before testing
    echo        5-AGENTIC.bat "your scenario"  tests anything you can describe
    echo.
    python -m agentic --scenario scenarios\controller_detected.yaml
) else (
    python -m agentic %*
)

set EXITCODE=%ERRORLEVEL%
echo.
echo ----------------------------------------------------------------------
REM Exit codes are the machine-readable verdict, for CI. Note that
REM `inconclusive` is deliberately NOT 0: a run that proved nothing must not
REM be able to turn a pipeline green.
if %EXITCODE%==0 echo VERDICT: PASS
if %EXITCODE%==1 echo VERDICT: FAIL          - see the report's root-cause section
if %EXITCODE%==2 echo VERDICT: BLOCKED       - the rig was not ready; nothing ran
if %EXITCODE%==3 echo VERDICT: INCONCLUSIVE  - it ran, but proved nothing either way
if %EXITCODE%==4 echo VERDICT: ERROR         - the harness itself failed
echo Reports are in Android-Agentic-Xcloud-Testing\reports\
echo ----------------------------------------------------------------------

popd
endlocal & exit /b %EXITCODE%
