@echo off
REM ============================================================
REM  Himal KB Mock Creator - sync pipeline + TTS scripts
REM  Run this AFTER updating the originals in ..\questions-creator
REM  or ~\.config\opencode\scripts, then rebuild the Docker image.
REM ============================================================
setlocal
set "BASE=%~dp0"
set "SRC=%BASE%..\questions-creator"

echo Syncing pipeline files from questions-creator...
copy /Y "%SRC%\mock_next.py"            "%BASE%pipeline\" >nul
copy /Y "%SRC%\mock_audio_builder.py"   "%BASE%pipeline\" >nul
copy /Y "%SRC%\magnific_mcp.py"         "%BASE%pipeline\" >nul
if errorlevel 1 goto :err

set "SCRIPTS=%USERPROFILE%\.config\opencode\scripts"
echo Syncing TTS scripts from %SCRIPTS%...
copy /Y "%SCRIPTS%\tts.py"          "%BASE%scripts\" >nul
copy /Y "%SCRIPTS%\audio_convert.py" "%BASE%scripts\" >nul
if errorlevel 1 goto :err

echo.
echo Done. Rebuild the image with:  docker build -t shivad90/mock-creator:latest .
exit /b 0

:err
echo.
echo FAILED - check the source folders above.
exit /b 1
