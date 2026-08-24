@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "SCANNER_PATH=%~dp0readonly_file_scanner.py"
if not exist "%SCANNER_PATH%" (
    echo [오류] readonly_file_scanner.py를 찾을 수 없습니다.
    pause
    exit /b 2
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_LAUNCHER=py"
) else (
    where python >nul 2>nul
    if not %errorlevel% equ 0 (
        echo [오류] Python 3을 찾을 수 없습니다.
        pause
        exit /b 2
    )
    set "PYTHON_LAUNCHER=python"
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$roots = [System.Collections.Generic.List[string]]::new();" ^
  "while ($true) {" ^
  "  $root = Read-Host '스캔할 폴더를 입력하십시오. 입력을 마치려면 빈 줄에서 Enter';" ^
  "  if ([string]::IsNullOrWhiteSpace($root)) { break };" ^
  "  $roots.Add($root.Trim());" ^
  "};" ^
  "if ($roots.Count -eq 0) { Write-Host '[오류] 스캔할 폴더를 하나 이상 입력해야 합니다.'; exit 2 };" ^
  "$output = Read-Host '새로 만들 SQLite 파일의 전체 경로를 입력하십시오';" ^
  "if ([string]::IsNullOrWhiteSpace($output)) { Write-Host '[오류] 출력 파일 경로가 비어 있습니다.'; exit 2 };" ^
  "$hashAnswer = Read-Host '크기가 같은 파일을 내용까지 비교하시겠습니까? (y/N)';" ^
  "$arguments = @($env:SCANNER_PATH);" ^
  "$arguments += $roots.ToArray();" ^
  "$arguments += @('--output', $output.Trim());" ^
  "if ($hashAnswer -match '^(y|yes|예)$') { $arguments += '--hash-duplicates' };" ^
  "if ($env:PYTHON_LAUNCHER -eq 'py') { & py -3 @arguments } else { & python @arguments };" ^
  "exit $LASTEXITCODE"

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if %EXIT_CODE% equ 0 (
    echo [완료] 스캔이 끝났습니다.
) else (
    echo [종료] 오류 또는 중단으로 끝났습니다. 종료 코드: %EXIT_CODE%
)
pause
exit /b %EXIT_CODE%
