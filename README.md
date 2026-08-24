# 오프라인 업무 파일 정리용 조회 스캐너

`offline-file-organizer`는 인터넷이 차단된 업무용 컴퓨터에서 파일과 폴더의 현황을 안전하게 조사하는 도구입니다. 원본 파일을 이동하거나 삭제하지 않고 파일명, 상대경로, 확장자, 용량, 생성·수정 시각 등을 보고서로 정리합니다. 이 보고서를 외부 인공지능에 전달하거나 사람이 직접 검토해 폴더 정리 기준을 세울 수 있습니다.

현재 저장소에는 **조회 전용 스캐너**만 들어 있습니다. 파일 이동, 이름 변경, 삭제 기능은 없습니다.

## 안전 원칙

- 스캔 대상의 파일과 폴더를 이동·삭제·이름 변경·내용 수정하지 않습니다.
- 보고서 상위 폴더가 스캔 대상 안에 있으면 실행을 거부합니다.
- 기본 모드는 파일 내용을 열지 않고 메타데이터만 조회합니다.
- `--hash-duplicates`를 지정한 경우에만 같은 크기의 파일을 읽기 전용으로 열어 내용 지문을 계산합니다.
- 파일형 클라우드 자리표시자와 재분석 지점은 목록에 남기되, 자동으로 내용을 읽지 않습니다.
- 심볼릭 링크와 폴더형 재분석 지점은 따라가지 않습니다.
- `cleanup_candidates.csv`는 검토 후보 목록입니다. 삭제 목록으로 사용하면 안 됩니다.

## 실행 환경

- Windows 10 또는 Windows 11
- Python 3.10 이상
- 외부 Python 패키지 불필요

설치된 Python 버전은 다음 명령으로 확인합니다.

```bat
py -3 --version
```

`py` 명령을 찾지 못하면 다음 명령을 사용합니다.

```bat
python --version
```

## 저장소 받기

인터넷에 연결된 컴퓨터에서는 다음 명령으로 저장소를 받을 수 있습니다.

```bash
git clone https://github.com/inkyu9111/offline-file-organizer.git
```

업무용 컴퓨터가 인터넷에 연결되지 않는다면 GitHub의 압축 파일을 내려받은 뒤, 회사에서 허용한 반입 절차에 따라 아래 파일을 업무용 컴퓨터로 옮깁니다.

```text
README.md
readonly_file_scanner.py
run_scanner.bat
test_readonly_file_scanner.py
```

실행에는 `readonly_file_scanner.py`만 필요합니다. `run_scanner.bat`는 명령 입력을 돕고, `test_readonly_file_scanner.py`는 프로그램 점검에 사용합니다.

## 가장 안전한 기본 실행

명령 프롬프트나 PowerShell을 열고 프로그램이 있는 폴더로 이동합니다.

```bat
cd /d "D:\도구\offline-file-organizer"
```

아래 예시는 `D:\업무자료`를 조회하고, 스캔 대상 밖인 `D:\파일정리_스캔결과`에 보고서를 만듭니다.

```bat
py -3 readonly_file_scanner.py "D:\업무자료" --output "D:\파일정리_스캔결과"
```

기본 실행에서는 파일 내용을 열지 않습니다. 실행할 때마다 보고서 상위 폴더 아래에 새 결과 폴더가 생깁니다.

```text
D:\파일정리_스캔결과\scan_20260824_143000
```

다음 명령은 출력 폴더가 스캔 대상 안에 있으므로 거부됩니다.

```bat
py -3 readonly_file_scanner.py "D:\업무자료" --output "D:\업무자료\스캔결과"
```

## 배치파일로 실행

`run_scanner.bat`와 `readonly_file_scanner.py`를 같은 폴더에 둡니다. `run_scanner.bat`를 더블클릭한 뒤 아래 항목을 차례로 입력합니다.

1. 조회할 업무 폴더
2. 보고서를 저장할 상위 폴더
3. 같은 크기의 파일을 내용까지 비교할지 여부

보고서 상위 폴더는 반드시 조회 대상 밖에 지정해야 합니다.

## 실제 중복 파일 확인

같은 크기의 파일 가운데 내용까지 같은 파일을 찾으려면 `--hash-duplicates`를 추가합니다.

```bat
py -3 readonly_file_scanner.py "D:\업무자료" ^
  --output "D:\파일정리_스캔결과" ^
  --hash-duplicates
```

기본값으로 파일 한 개가 4 GiB를 넘으면 내용 지문을 계산하지 않습니다. 최대 크기를 1 GiB로 줄이려면 다음과 같이 실행합니다.

```bat
py -3 readonly_file_scanner.py "D:\업무자료" ^
  --output "D:\파일정리_스캔결과" ^
  --hash-duplicates ^
  --max-hash-size-mb 1024
```

크기 제한을 없애려면 `0`을 지정합니다.

```bat
--max-hash-size-mb 0
```

내용 비교는 원본을 수정하지 않지만 파일을 끝까지 읽습니다. 대용량 파일이나 네트워크 드라이브를 대상으로 실행하면 시간이 오래 걸릴 수 있습니다. 처음에는 `--hash-duplicates` 없이 전체 현황부터 확인하는 편이 안전합니다.

## 제외 규칙

기본 실행은 아래 기술·캐시 폴더를 제외합니다.

```text
.git, .svn, .hg, .venv, venv, env, node_modules, __pycache__,
.pytest_cache, .mypy_cache, .ruff_cache, .tox, .cache,
$RECYCLE.BIN, System Volume Information
```

사용자 제외 규칙은 `--exclude`를 여러 번 지정해 추가합니다.

```bat
py -3 readonly_file_scanner.py "D:\업무자료" ^
  --output "D:\파일정리_스캔결과" ^
  --exclude "완료/**" ^
  --exclude "*.iso"
```

기본 제외 폴더까지 모두 조회하려면 다음 옵션을 사용합니다.

```bat
--no-default-excludes
```

## 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--output PATH` | 보고서 상위 폴더를 지정합니다. 필수이며 스캔 대상 밖이어야 합니다. |
| `--hash-duplicates` | 같은 크기의 파일을 내용 지문으로 한 번 더 비교합니다. |
| `--max-hash-size-mb N` | 내용 지문을 계산할 파일 한 개의 최대 크기를 정합니다. 기본값은 4096 MiB이며 `0`은 무제한입니다. |
| `--old-days N` | 오래된 파일 후보로 분류할 기준 일수를 정합니다. 기본값은 730일입니다. |
| `--large-count N` | 대용량 파일 보고서에 넣을 상위 파일 수를 정합니다. 기본값은 100개입니다. |
| `--exclude PATTERN` | 제외할 상대경로 무늬를 추가합니다. 여러 번 사용할 수 있습니다. |
| `--no-default-excludes` | 기본 기술 폴더 제외 규칙을 해제합니다. |
| `--progress-every N` | 파일 `N`개마다 진행 상황을 표시합니다. `0`이면 표시하지 않습니다. |
| `--help` | 전체 도움말을 표시합니다. |

전체 도움말은 다음 명령으로 확인합니다.

```bat
py -3 readonly_file_scanner.py --help
```

## 생성되는 보고서

| 파일 | 내용 |
|---|---|
| `README_FIRST.txt` | 결과 확인 순서와 주의사항 |
| `inventory_summary.txt` | 전체 파일 수, 총용량, 유형, 오류, 중복 요약 |
| `folder_tree.txt` | 파일 식별자, 유형, 용량, 수정일을 포함한 계층형 구조 |
| `file_manifest.csv` | 표 계산 프로그램과 인공지능 분석에 쓸 전체 파일 목록 |
| `file_manifest.jsonl` | 원래 문자열을 보존한 기계 판독용 전체 목록 |
| `folder_summary.csv` | 폴더별 직접·하위 파일 수와 용량 |
| `empty_folders.csv` | 실제로 항목이 없는 빈 폴더 |
| `large_files.csv` | 용량이 큰 상위 파일 |
| `old_files.csv` | 기준 일수 이상 수정되지 않은 파일 |
| `cleanup_candidates.csv` | 임시·백업·오래된 파일 등의 사람 검토 후보 |
| `same_size_candidates.csv` | 크기가 같은 파일 후보 |
| `confirmed_duplicates.csv` | 내용 지문까지 같은 파일 |
| `excluded_paths.csv` | 규칙과 링크 처리 때문에 제외된 경로 |
| `scan_errors.csv` | 권한 부족과 읽기 오류 등의 기록 |
| `scan_metadata.json` | 실행 설정과 결과 건수 요약 |
| `AI_ANALYSIS_PROMPT.txt` | 폴더 구조와 이동안을 인공지능에 요청할 때 쓸 지시문 |

쉼표 구분 파일은 한국어 Windows의 Excel에서 바로 열기 쉽도록 UTF-8 BOM으로 저장합니다. 파일명이 `=`, `+`, `-`, `@` 등으로 시작하면 수식으로 실행되지 않도록 표시값 앞에 작은따옴표를 붙입니다. 원래 파일명은 `file_manifest.jsonl`에 남습니다.

## 인공지능에 전달할 자료

처음부터 전체 파일 목록을 넘기지 말고 다음 순서로 확인하는 편이 좋습니다.

1. `inventory_summary.txt`
2. `folder_tree.txt`
3. `folder_summary.csv`
4. 상세 분류가 필요할 때만 `file_manifest.csv`
5. `AI_ANALYSIS_PROMPT.txt`

보고서에는 파일 내용이 들어가지 않습니다. 다만 파일명과 폴더명만으로도 설비명, 사업명, 직원명, 계약 정보 같은 업무정보가 드러날 수 있습니다. 외부 인공지능에 입력하기 전에 회사의 정보 반출 규정과 허용 범위를 확인해야 합니다.

## 테스트 실행

저장소 폴더에서 다음 명령을 실행합니다.

```bat
py -3 -m unittest -v test_readonly_file_scanner.py
```

`py` 명령을 사용할 수 없는 환경에서는 다음과 같이 실행합니다.

```bat
python -m unittest -v test_readonly_file_scanner.py
```

테스트는 원본 비변경, 출력 경로 차단, 기본 제외 규칙, 선택적 중복 확인, 보고서 생성, 쉼표 구분 파일 안전 처리 등을 점검합니다.

## 결과 해석 시 주의할 점

- `size_bytes`는 논리적 파일 크기입니다. 압축 파일과 희소 파일이 실제 디스크에서 차지하는 용량과 다를 수 있습니다.
- Linux와 macOS에서는 파일시스템이 생성 시각을 제공하지 않을 수 있습니다. 이 경우 `created_time_source`에 메타데이터 변경 시각이 표시됩니다.
- `same_size_candidates.csv`에 함께 나온 파일은 크기만 같을 수 있습니다. 실제 중복 여부는 내용 지문 확인이 필요합니다.
- 내용 지문이 같아도 어느 파일을 남길지, 다른 문서가 기존 경로를 참조하는지는 사람이 판단해야 합니다.
- Excel 외부 링크, 바로가기, 배치파일, 프로그램 설정파일의 경로 의존성은 이 조회 단계에서 바꾸지 않습니다.
- 네트워크 공유 폴더를 조회할 때는 다른 사용자의 작업과 접근 권한을 고려해야 합니다.

## 저장소 구성

```text
.
├── README.md
├── readonly_file_scanner.py
├── run_scanner.bat
└── test_readonly_file_scanner.py
```
