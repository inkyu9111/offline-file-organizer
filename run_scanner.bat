@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

echo ============================================================
echo 오프라인 업무 파일 조회 스캐너
echo 원본 파일을 이동, 삭제, 이름 변경 또는 수정하지 않습니다.
echo 여러 폴더의 결과를 SQLite 파일 하나에 저장합니다.
echo ============================================================
echo.

set "ROOT_ARGS="
set /a ROOT_COUNT=0

:ASK_ROOT
set "SCAN_ROOT="
set /p "SCAN_ROOT=스캔할 폴더를 입력하십시오. 입력을 마치려면 빈 줄에서 Enter: "
if not defined SCAN_ROOT goto ROOT_INPUT_DONE
set /a ROOT_COUNT+=1
set "ROOT_ARGS=!ROOT_ARGS! --root "!SCAN_ROOT!""
goto ASK_ROOT

:ROOT_INPUT_DONE
if !ROOT_COUNT! LSS 1 (
    echo [오류] 스캔할 폴더를 하나 이상 입력해야 합니다.
    pause
    exit /b 2
)

echo.
set "OUTPUT_DB="
set /p "OUTPUT_DB=새로 만들 결과 파일 경로를 입력하십시오. 예: F:\파일정리결과\scan.sqlite3: "
if not defined OUTPUT_DB (
    echo [오류] 결과 파일 경로를 입력하지 않았습니다.
    pause
    exit /b 2
)

echo.
set "HASH_CHOICE="
set /p "HASH_CHOICE=같은 크기의 파일을 SHA-256으로 비교하시겠습니까? [y/N]: "
set "HASH_OPTION="
if /I "!HASH_CHOICE!"=="Y" set "HASH_OPTION=--hash-duplicates"
if /I "!HASH_CHOICE!"=="YES" set "HASH_OPTION=--hash-duplicates"

echo.
where py >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    py -3 "%~dp0readonly_file_scanner.py" !ROOT_ARGS! --output "!OUTPUT_DB!" !HASH_OPTION!
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [오류] Python 3.10 이상을 찾지 못했습니다.
        echo 승인된 Python 환경을 설치하거나 복사한 뒤 다시 실행하십시오.
        pause
        exit /b 2
    )
    python "%~dp0readonly_file_scanner.py" !ROOT_ARGS! --output "!OUTPUT_DB!" !HASH_OPTION!
)

set "EXIT_CODE=!ERRORLEVEL!"
echo.
if "!EXIT_CODE!"=="0" (
    echo 스캔이 끝났습니다. 결과 SQLite 파일의 scan_run 표부터 확인하십시오.
) else if "!EXIT_CODE!"=="130" (
    echo 사용자가 작업을 중단했습니다. 결과 파일에 마지막 진행 상태가 남아 있습니다.
) else (
    echo 스캔이 종료 코드 !EXIT_CODE!로 끝났습니다.
    echo 결과 파일이 만들어졌다면 scan_run과 scan_errors 표를 확인하십시오.
)
echo.
pause
exit /b !EXIT_CODE!
