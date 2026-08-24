# 오프라인 업무 파일 조회 스캐너

`offline-file-organizer`는 인터넷이 차단된 업무용 컴퓨터에서 여러 폴더의 파일 현황을 조사하는 도구입니다. 원본을 옮기거나 지우지 않으며, 조사 결과는 쉼표 구분 파일 여러 개가 아니라 **하나의 SQLite 데이터베이스**에 저장합니다.

스캔 중 발견한 파일과 오류는 바로 데이터베이스에 기록합니다. 작업이 오래 걸리거나 중간에 멈춘 경우에도 `scan_run` 표에서 마지막으로 처리하던 루트, 폴더, 작업 단계와 누적 건수를 확인할 수 있습니다.

## 안전 범위

- 스캔 대상의 파일과 폴더를 이동·삭제·이름 변경·수정하지 않습니다.
- 출력 데이터베이스가 스캔 대상 안에 있으면 실행을 거부합니다.
- 서로 겹치는 스캔 폴더도 거부합니다. 상위 폴더와 그 하위 폴더를 함께 지정하면 같은 파일이 두 번 잡히기 때문입니다.
- 기본 모드에서는 파일 내용을 열지 않고 이름, 상대경로, 확장자, 용량, 생성·수정 시각 같은 메타데이터만 읽습니다.
- `--hash-duplicates`를 지정한 경우에만 같은 크기의 후보 파일을 읽기 전용으로 열어 SHA-256을 계산합니다.
- 심볼릭 링크와 폴더형 재분석 지점은 따라가지 않습니다.
- 정리 후보는 사람이 검토할 자료입니다. 프로그램은 삭제 대상을 만들거나 파일을 자동으로 정리하지 않습니다.

## 실행 환경

- Windows 10 또는 Windows 11
- Python 3.10 이상
- 별도 Python 패키지 불필요

Python 설치 여부는 다음 명령으로 확인합니다.

```bat
py -3 --version
```

`py` 명령이 없다면 다음 명령도 확인하십시오.

```bat
python --version
```

## 여러 폴더를 한 번에 스캔하기

다음 예시는 `D:\업무자료1`과 `E:\업무자료2`를 함께 조사해 `F:\파일정리결과\scan.sqlite3` 하나에 저장합니다.

```bat
py -3 readonly_file_scanner.py "D:\업무자료1" "E:\업무자료2" --output "F:\파일정리결과\scan.sqlite3"
```

스캔 폴더는 세 개 이상도 같은 방식으로 이어서 적을 수 있습니다.

```bat
py -3 readonly_file_scanner.py ^
  "D:\업무자료1" ^
  "E:\업무자료2" ^
  "F:\공용참고자료" ^
  --output "G:\파일정리결과\scan.sqlite3"
```

폴더 하나만 지정하는 기존 방식도 지원합니다.

```bat
py -3 readonly_file_scanner.py "D:\업무자료1" --output "F:\파일정리결과\scan.sqlite3"
```

출력 파일이 이미 있으면 덮어쓰지 않고 종료합니다. 기존 결과를 보존한 채 새 파일명을 사용하십시오.

## 배치파일로 실행하기

`run_scanner.bat`와 `readonly_file_scanner.py`를 같은 폴더에 둔 뒤 배치파일을 실행합니다. 스캔할 폴더를 한 줄씩 입력하고, 입력이 끝나면 빈 줄에서 Enter를 누릅니다. 배치파일은 내부적으로 `--root` 옵션을 반복해서 전달합니다.

명령행에서 같은 방식을 직접 사용할 수도 있습니다.

```bat
py -3 readonly_file_scanner.py ^
  --root "D:\업무자료1" ^
  --root "E:\업무자료2" ^
  --output "F:\파일정리결과\scan.sqlite3"
```

## 진행 상태 확인

실행 중에는 다음과 비슷한 문구가 갱신됩니다.

```text
[진행] 루트 1/2 ROOT-001 | 현재 폴더 사업/2026/보고서 | 완료 폴더 118/발견 243 | 파일 12,420개 | 8.31 GiB | 제외 9개 | 오류 2건 | 경과 00:04:18 | 폴더 항목 조회
```

표시되는 값은 다음 뜻입니다.

- `현재 폴더`: 프로그램이 지금 `os.scandir()`로 읽는 폴더
- `완료 폴더`: 항목 조회가 끝난 폴더 수
- `발견`: 현재까지 찾은 전체 폴더 수이며, 아직 처리하지 않은 폴더도 포함
- `파일`: 데이터베이스에 기록한 파일 수
- `오류`: 접근 권한이나 파일 변경 등으로 기록된 오류 수

전체 폴더 수는 스캔을 끝내기 전에는 알 수 없으므로 고정된 백분율은 표시하지 않습니다. 대신 완료·발견·대기 상태가 계속 갱신됩니다. 대형 폴더나 네트워크 경로에서 작업이 멈춘 것처럼 보여도 마지막 줄의 `현재 폴더`를 보면 어느 위치에서 기다리는지 알 수 있습니다.

기본값은 1초 또는 파일 250개마다 화면을 갱신합니다.

```bat
--progress-interval 1
--progress-every 250
```

모든 변화를 출력하려면 다음과 같이 지정할 수 있습니다. 폴더와 파일이 많으면 화면 출력이 매우 길어집니다.

```bat
--progress-interval 0 --progress-every 1
```

## 중간에 멈췄을 때 확인할 내용

데이터베이스의 `scan_run` 표에는 진행 상태가 계속 저장됩니다. SQLite 조회 프로그램이나 Python에서 다음 질의를 실행하십시오.

```sql
SELECT
    status,
    phase,
    current_root_label,
    current_folder,
    current_operation,
    last_heartbeat_at,
    folders_discovered,
    folders_completed,
    files_scanned,
    error_count,
    failure_type,
    failure_message
FROM scan_run
WHERE run_id = 1;
```

상태 값은 다음과 같습니다.

| 상태 | 뜻 |
|---|---|
| `running` | 아직 실행 중이거나 강제 종료되어 정상 마감 기록을 남기지 못함 |
| `completed` | 오류 없이 완료 |
| `completed_with_errors` | 일부 항목에서 오류가 났지만 전체 절차는 완료 |
| `interrupted` | Ctrl+C로 중단 |
| `failed` | 예상하지 못한 예외로 중단 |

세부 오류는 `scan_errors`, 폴더 시작·완료 이력은 `scan_events`에서 확인합니다. 예상하지 못한 예외가 발생하면 `failure_type`, `failure_message`, `failure_traceback`에도 원인이 남습니다.

## 발견 즉시 저장하는 방식

파일을 전부 메모리에 모은 뒤 한꺼번에 쓰지 않습니다.

1. 출력 SQLite 파일과 표를 먼저 만듭니다.
2. 폴더를 열기 직전에 `current_folder`를 기록하고 변경 내용을 확정합니다.
3. 파일을 발견하면 `files`에 바로 추가합니다.
4. 제외 항목과 오류도 각각 `excluded_paths`, `scan_errors`에 바로 추가합니다.
5. 기본값으로 쓰기 100건마다 변경 내용을 확정하며, 진행 상태를 화면에 내보낼 때도 확정합니다.
6. 전체 스캔이 끝난 뒤에는 데이터베이스 질의로 폴더 하위 집계와 중복 그룹만 계산합니다.
7. 폴더 하위 집계처럼 SQLite가 만드는 임시 작업도 디스크를 사용합니다.

더 자주 확정하려면 `--commit-every` 값을 줄입니다.

```bat
--commit-every 10
```

한 건마다 확정하는 설정은 중단 시 손실 범위를 가장 작게 만들지만 느려질 수 있습니다.

```bat
--commit-every 1
```

정상 종료 후에는 `scan.sqlite3` 하나만 남습니다. 실행 중에는 SQLite가 복구용 `-journal` 파일을 잠시 만들 수 있으며, 비정상 종료 시 복구를 위해 남을 수도 있습니다. 이 경우 원본 데이터베이스와 함께 보관한 뒤 SQLite로 다시 열어 복구를 마치십시오.

## 실제 중복 파일 확인

파일 크기만 같은 후보를 내용까지 비교하려면 `--hash-duplicates`를 추가합니다.

```bat
py -3 readonly_file_scanner.py ^
  "D:\업무자료1" "E:\업무자료2" ^
  --output "F:\파일정리결과\scan.sqlite3" ^
  --hash-duplicates
```

기본값으로 파일 한 개가 4 GiB를 넘으면 해시하지 않습니다. 최대 크기를 1 GiB로 낮추려면 다음 옵션을 붙입니다.

```bat
--max-hash-size-mb 1024
```

제한을 없애려면 `0`을 지정합니다.

```bat
--max-hash-size-mb 0
```

해시 모드는 파일을 수정하지 않지만 내용을 끝까지 읽습니다. 대용량 파일이나 네트워크 드라이브에서는 시간이 오래 걸릴 수 있습니다. 진행 문구의 `중복 확인 완료/전체` 수치로 처리 현황을 확인할 수 있습니다.

## 제외 규칙

기본 실행은 아래 기술·캐시 폴더를 제외합니다.

```text
.git, .svn, .hg, .venv, venv, env, node_modules, __pycache__,
.pytest_cache, .mypy_cache, .ruff_cache, .tox, .cache,
$RECYCLE.BIN, System Volume Information
```

사용자 제외 규칙은 여러 번 지정할 수 있습니다.

```bat
py -3 readonly_file_scanner.py ^
  "D:\업무자료1" "E:\업무자료2" ^
  --output "F:\파일정리결과\scan.sqlite3" ^
  --exclude "완료/**" ^
  --exclude "*.iso"
```

기본 제외 규칙을 해제하려면 다음 옵션을 사용합니다.

```bat
--no-default-excludes
```

## 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--output FILE.sqlite3` | 새로 만들 결과 파일. 모든 스캔 폴더 밖에 지정해야 함 |
| `--root PATH` | 스캔 폴더를 반복해서 추가. 배치파일에서 사용 |
| `--hash-duplicates` | 같은 크기의 후보 파일을 SHA-256으로 확인 |
| `--max-hash-size-mb N` | 해시할 파일 한 개의 최대 크기. 기본 4096 MiB, `0`은 무제한 |
| `--old-days N` | 오래된 파일 후보 기준 일수. 기본 730일 |
| `--large-count N` | `v_large_files`에 표시할 상위 파일 수. 기본 100개 |
| `--exclude PATTERN` | 제외할 상대경로 무늬. 여러 번 사용 가능 |
| `--no-default-excludes` | 기본 기술 폴더 제외 규칙 해제 |
| `--progress-interval N` | 화면 진행 갱신 최대 간격. 기본 1초 |
| `--progress-every N` | 파일 N개마다 진행 갱신. 기본 250개 |
| `--commit-every N` | 쓰기 N건마다 데이터베이스 변경 확정. 기본 100건 |
| `--help` | 전체 도움말 표시 |

전체 도움말은 다음 명령으로 확인합니다.

```bat
py -3 readonly_file_scanner.py --help
```

## SQLite 구성

자주 확인할 표와 보기는 다음과 같습니다.

| 이름 | 종류 | 내용 |
|---|---|---|
| `scan_run` | 표 | 전체 상태, 현재 폴더, 누적 건수, 실패 원인 |
| `scan_roots` | 표 | 입력 루트별 상태와 집계 |
| `folders` | 표 | 폴더별 직접·하위 집계와 처리 상태 |
| `files` | 표 | 파일별 메타데이터와 해시 상태 |
| `cleanup_candidates` | 표 | 사람이 확인할 정리 후보 |
| `excluded_paths` | 표 | 제외한 경로와 사유 |
| `scan_errors` | 표 | 접근·조회·해시 오류 |
| `scan_events` | 표 | 폴더 시작·완료와 주요 단계 이력 |
| `v_file_manifest` | 보기 | 루트 이름을 붙인 전체 파일 목록 |
| `v_folder_summary` | 보기 | 루트 이름을 붙인 폴더 요약 |
| `v_empty_folders` | 보기 | 실제 빈 폴더 |
| `v_old_files` | 보기 | 설정 기준보다 오래된 파일 |
| `v_large_files` | 보기 | 용량이 큰 상위 파일 |
| `v_same_size_candidates` | 보기 | 크기가 같은 후보 |
| `v_confirmed_duplicates` | 보기 | SHA-256까지 같은 파일 |
| `embedded_documents` | 표 | 데이터베이스 안내문과 인공지능 분석 지시문 |

예를 들어 전체 파일 목록은 다음 질의로 읽습니다.

```sql
SELECT *
FROM v_file_manifest
ORDER BY root_label, relative_path;
```

마지막 오류 20건은 다음과 같이 확인합니다.

```sql
SELECT occurred_at, root_id, relative_path, operation, error_type, message
FROM scan_errors
ORDER BY error_id DESC
LIMIT 20;
```

## 인공지능에 전달할 때

`embedded_documents` 표의 `AI_ANALYSIS_PROMPT`와 필요한 보기를 함께 사용합니다. 처음에는 `scan_run`, `scan_roots`, `v_folder_summary`만 확인하고, 파일별 분류가 필요할 때 `v_file_manifest`를 읽는 편이 낫습니다.

파일 내용은 데이터베이스에 들어가지 않습니다. 다만 파일명과 폴더명만으로도 설비명, 사업명, 직원명, 계약 정보가 드러날 수 있습니다. 외부 인공지능에 전달하기 전에 회사의 정보 반출 규정과 허용 범위를 확인하십시오.

## 테스트 실행

```bat
py -3 -m unittest -v test_readonly_file_scanner.py
```

테스트는 다중 루트, 출력 경로 차단, 발견 즉시 기록, 진행 상태 저장, 중단 원인 기록, 폴더 하위 집계, 중복 해시, 한글 역할 주석을 점검합니다.

## 저장소 구성

```text
.
├── README.md
├── readonly_file_scanner.py
├── run_scanner.bat
└── test_readonly_file_scanner.py
```
