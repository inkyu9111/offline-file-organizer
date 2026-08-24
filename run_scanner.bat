@echo off
setlocal EnableExtensions DisableDelayedExpansion

echo ============================================================
echo Read-only File and Folder Scanner
echo This tool does NOT move, rename, edit, or delete source files.
echo The report folder MUST be outside the scan folder.
echo ============================================================
echo.

set /p "SCAN_ROOT=Enter the folder to scan: "
if not defined SCAN_ROOT (
    echo [ERROR] Scan folder was not entered.
    pause
    exit /b 2
)

set /p "REPORT_ROOT=Enter the report parent folder: "
if not defined REPORT_ROOT (
    echo [ERROR] Report folder was not entered.
    pause
    exit /b 2
)

echo.
set /p "HASH_CHOICE=Confirm same-size files with SHA-256? [y/N]: "
set "HASH_OPTION="
if /I "%HASH_CHOICE%"=="Y" set "HASH_OPTION=--hash-duplicates"
if /I "%HASH_CHOICE%"=="YES" set "HASH_OPTION=--hash-duplicates"

echo.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%~dp0readonly_file_scanner.py" "%SCAN_ROOT%" --output "%REPORT_ROOT%" %HASH_OPTION%
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python 3.10 or newer was not found.
        echo Install or copy an approved Python environment, then try again.
        pause
        exit /b 2
    )
    python "%~dp0readonly_file_scanner.py" "%SCAN_ROOT%" --output "%REPORT_ROOT%" %HASH_OPTION%
)

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo Scan completed. Review README_FIRST.txt in the generated report folder.
) else (
    echo Scan failed with exit code %EXIT_CODE%.
)
echo.
pause
exit /b %EXIT_CODE%
