@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

echo ============================================================
echo 오프라인 업무 파일 조회 스캐너
echo 원본 파일을 이동, 삭제, 이름 변경 또는 수정하지 않습니다.
echo 여러 폴더의 결과를 SQLite 파일 하나에 저장합니다.
echo ============================================================
echo.

where py >nul 2>&1
if errorlevel 1 goto TRY_PYTHON

py -3 -X utf8 "%~dp0readonly_file_scanner.py" --interactive
set "EXIT_CODE=%ERRORLEVEL%"
goto FINISH

:TRY_PYTHON
where python >nul 2>&1
if errorlevel 1 goto PYTHON_NOT_FOUND

python -X utf8 "%~dp0readonly_file_scanner.py" --interactive
set "EXIT_CODE=%ERRORLEVEL%"
goto FINISH

:PYTHON_NOT_FOUND
echo [오류] Python 3.10 이상을 찾지 못했습니다.
echo 승인된 Python 환경을 설치하거나 복사한 뒤 다시 실행하십시오.
set "EXIT_CODE=2"

:FINISH
echo.
if "%EXIT_CODE%"=="0" goto SUCCESS
if "%EXIT_CODE%"=="130" goto INTERRUPTED

echo 스캔이 종료 코드 %EXIT_CODE%로 끝났습니다.
echo 결과 파일이 만들어졌다면 scan_run과 scan_errors 표를 확인하십시오.
goto END

:SUCCESS
echo 스캔이 끝났습니다. 결과 SQLite 파일의 scan_run 표부터 확인하십시오.
goto END

:INTERRUPTED
echo 사용자가 작업을 중단했습니다. 결과 파일에 마지막 진행 상태가 남아 있습니다.

:END
echo.
pause
exit /b %EXIT_CODE%
