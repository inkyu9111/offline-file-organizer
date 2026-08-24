#!/usr/bin/env python3
"""여러 업무 폴더를 읽기 전용으로 조사해 단일 SQLite 파일에 기록한다.

스캔 대상에는 파일 생성, 수정, 이동, 이름 변경, 삭제 작업을 하지 않는다.
기본 모드는 파일 내용을 열지 않으며, 중복 확인 옵션을 켠 경우에만 같은
크기의 후보 파일을 읽기 전용으로 열어 SHA-256을 계산한다.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat as stat_module
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, TextIO


SCANNER_VERSION = "2.0.0"
DATABASE_SCHEMA_VERSION = 2

DEFAULT_EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".cache",
        "$recycle.bin",
        "system volume information",
    }
)

_EXTENSION_CATEGORIES: dict[str, set[str]] = {
    "document": {
        ".doc",
        ".docx",
        ".docm",
        ".hwp",
        ".hwpx",
        ".odt",
        ".rtf",
        ".txt",
        ".md",
    },
    "spreadsheet": {
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xlsb",
        ".csv",
        ".tsv",
        ".ods",
    },
    "presentation": {".ppt", ".pptx", ".pptm", ".odp"},
    "pdf": {".pdf"},
    "image": {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".webp",
        ".svg",
        ".heic",
    },
    "archive": {
        ".zip",
        ".7z",
        ".rar",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".tgz",
        ".cab",
    },
    "executable_installer": {
        ".exe",
        ".msi",
        ".msix",
        ".appx",
        ".appxbundle",
        ".com",
        ".scr",
    },
    "script": {
        ".py",
        ".pyw",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".vbs",
        ".js",
        ".ts",
    },
    "data": {
        ".json",
        ".jsonl",
        ".xml",
        ".yaml",
        ".yml",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".parquet",
        ".feather",
    },
    "email": {".msg", ".eml", ".pst", ".ost"},
    "shortcut": {".lnk", ".url"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"},
    "video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"},
    "font": {".ttf", ".otf", ".woff", ".woff2"},
}

_TEMP_OR_BACKUP_EXTENSIONS = frozenset(
    {
        ".tmp",
        ".temp",
        ".bak",
        ".backup",
        ".old",
        ".orig",
        ".part",
        ".crdownload",
        ".download",
        ".dmp",
    }
)

_COPY_NAME_RE = re.compile(
    r"(?:복사본|사본|copy|duplicate|\(\d+\)|_\d+)$", re.IGNORECASE
)


# 스캔 대상과 출력 방식에 필요한 설정값을 보관한다.
@dataclass(slots=True)
class ScannerConfig:
    roots: tuple[Path | str, ...]
    output_db: Path | str
    hash_mode: str = "none"
    max_hash_size_bytes: int = 4 * 1024**3
    old_days: int = 730
    large_count: int = 100
    exclude_patterns: tuple[str, ...] = ()
    use_default_excludes: bool = True
    progress_interval_seconds: float = 1.0
    progress_every_files: int = 250
    commit_every: int = 100

    # 설정값의 형식과 허용 범위를 검사한다.
    def __post_init__(self) -> None:
        self.roots = tuple(Path(root) for root in self.roots)
        self.output_db = Path(self.output_db)
        self.exclude_patterns = tuple(pattern for pattern in self.exclude_patterns if pattern)
        if not self.roots:
            raise ValueError("스캔 폴더를 하나 이상 지정해야 합니다.")
        if self.hash_mode not in {"none", "duplicates"}:
            raise ValueError("hash_mode는 none 또는 duplicates여야 합니다.")
        if self.max_hash_size_bytes < 0:
            raise ValueError("max_hash_size_bytes는 0 이상이어야 합니다.")
        if self.old_days < 0:
            raise ValueError("old_days는 0 이상이어야 합니다.")
        if self.large_count < 1:
            raise ValueError("large_count는 1 이상이어야 합니다.")
        if self.progress_interval_seconds < 0:
            raise ValueError("progress_interval_seconds는 0 이상이어야 합니다.")
        if self.progress_every_files < 0:
            raise ValueError("progress_every_files는 0 이상이어야 합니다.")
        if self.commit_every < 1:
            raise ValueError("commit_every는 1 이상이어야 합니다.")


# 실행 중 표시하고 데이터베이스에 저장할 진행 상태를 담는다.
@dataclass(slots=True)
class ProgressSnapshot:
    phase: str
    roots_total: int
    roots_completed: int = 0
    current_root_id: int | None = None
    current_root_label: str = ""
    current_folder: str = ""
    current_operation: str = ""
    folders_discovered: int = 0
    folders_completed: int = 0
    files_scanned: int = 0
    bytes_scanned: int = 0
    excluded_count: int = 0
    error_count: int = 0
    cleanup_rule_hits: int = 0
    hash_candidates_total: int = 0
    hash_files_completed: int = 0
    elapsed_seconds: float = 0.0


# 스캔이 끝난 뒤 사용자에게 돌려줄 핵심 결과를 담는다.
@dataclass(slots=True)
class ScanSummary:
    output_db: Path
    status: str
    root_count: int
    folder_count: int
    file_count: int
    byte_count: int
    excluded_count: int
    error_count: int
    duplicate_group_count: int
    elapsed_seconds: float


# 데이터베이스에 등록한 스캔 루트의 내부 정보를 보관한다.
@dataclass(slots=True)
class _RootContext:
    root_id: int
    root_label: str
    root_key: str
    absolute_path: Path
    error_count_at_start: int


# 절대경로를 바탕으로 스캔 루트의 안정적인 내부 식별자를 만든다.
def stable_root_key(path: Path | str) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    normalized = os.path.normcase(str(resolved)).replace("\\", "/").casefold()
    digest = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"R-{digest[:16].upper()}"


# 루트 식별자와 상대경로를 묶어 파일 식별자를 만든다.
def stable_file_id(root_key: str, relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").strip("/").casefold()
    payload = f"{root_key}\0{normalized}"
    digest = hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"F-{digest[:20].upper()}"


# 확장자를 업무 파일 유형으로 분류한다.
def classify_extension(extension: str) -> str:
    normalized = extension.casefold()
    if not normalized:
        return "no_extension"
    for category, extensions in _EXTENSION_CATEGORIES.items():
        if normalized in extensions:
            return category
    return "other"


# 바이트 수를 사람이 읽기 쉬운 이진 단위로 바꾼다.
def human_size(size_bytes: int) -> str:
    if size_bytes < 0:
        raise ValueError("size_bytes는 0 이상이어야 합니다.")
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(size_bytes)
    for index, unit in enumerate(units):
        if value < 1024.0 or index == len(units) - 1:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    raise AssertionError("도달할 수 없는 분기입니다.")


# 한 경로가 다른 경로 안에 들어 있는지 판별한다.
def _is_within(child: Path, parent: Path) -> bool:
    child_text = os.path.normcase(str(child))
    parent_text = os.path.normcase(str(parent))
    try:
        return os.path.commonpath([child_text, parent_text]) == parent_text
    except ValueError:
        return False


# 여러 스캔 폴더와 출력 데이터베이스 경로의 안전성을 검사한다.
def validate_scan_paths(
    roots: Sequence[Path | str], output_db: Path | str
) -> tuple[tuple[Path, ...], Path]:
    if not roots:
        raise ValueError("스캔 폴더를 하나 이상 지정해야 합니다.")

    resolved_roots: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve(strict=False)
        if not resolved.exists():
            raise FileNotFoundError(f"스캔 폴더가 없습니다: {resolved}")
        if not resolved.is_dir():
            raise NotADirectoryError(f"스캔 대상이 폴더가 아닙니다: {resolved}")
        resolved_roots.append(resolved)

    normalized_roots = [os.path.normcase(str(root)) for root in resolved_roots]
    if len(set(normalized_roots)) != len(normalized_roots):
        raise ValueError("같은 스캔 폴더가 중복 지정되었습니다.")

    for index, first in enumerate(resolved_roots):
        for second in resolved_roots[index + 1 :]:
            if _is_within(first, second) or _is_within(second, first):
                raise ValueError(
                    "스캔 폴더끼리 포함 관계가 있습니다. 상위 폴더와 하위 폴더를 "
                    "함께 지정하면 같은 파일을 두 번 조사할 수 있습니다."
                )

    resolved_output = Path(output_db).expanduser().resolve(strict=False)
    if resolved_output.exists():
        raise FileExistsError(f"출력 데이터베이스가 이미 존재합니다: {resolved_output}")
    for root in resolved_roots:
        if _is_within(resolved_output, root):
            raise ValueError("출력 데이터베이스는 모든 스캔 대상 밖에 지정해야 합니다.")
    if resolved_output.name in {"", ".", ".."}:
        raise ValueError("출력 데이터베이스 파일명을 지정해야 합니다.")
    if resolved_output.parent.exists() and not resolved_output.parent.is_dir():
        raise NotADirectoryError(f"출력 상위 경로가 폴더가 아닙니다: {resolved_output.parent}")
    return tuple(resolved_roots), resolved_output


# 현재 시각을 현지 시간 기준 문자열로 만든다.
def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# 시각 값을 현지 시간 기준 문자열로 바꾼다.
def _format_local_timestamp(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


# 상대경로의 상위 폴더를 구한다.
def _parent_relative(relative_path: str) -> str:
    if relative_path == ".":
        return "."
    parent = PurePosixPath(relative_path).parent.as_posix()
    return "." if parent in {"", "."} else parent


# 상위 폴더와 항목 이름을 안전한 상대경로로 잇는다.
def _join_relative(parent: str, name: str) -> str:
    return name if parent == "." else f"{parent}/{name}"


# 상대경로를 실제 폴더 경로로 복원한다.
def _absolute_from_relative(root: Path, relative_path: str) -> Path:
    if relative_path == ".":
        return root
    return root.joinpath(*PurePosixPath(relative_path).parts)


# 운영체제의 숨김, 시스템, 재분석 지점 속성을 읽는다.
def _windows_attributes(stat_result: os.stat_result) -> tuple[bool, bool, bool]:
    attributes = int(getattr(stat_result, "st_file_attributes", 0) or 0)
    hidden_mask = int(getattr(stat_module, "FILE_ATTRIBUTE_HIDDEN", 0) or 0)
    system_mask = int(getattr(stat_module, "FILE_ATTRIBUTE_SYSTEM", 0) or 0)
    reparse_mask = int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return (
        bool(attributes & hidden_mask),
        bool(attributes & system_mask),
        bool(attributes & reparse_mask),
    )


# 파일 속성에서 숨김, 시스템, 읽기 전용 여부를 판별한다.
def _item_flags(name: str, stat_result: os.stat_result) -> tuple[bool, bool, bool, bool]:
    windows_hidden, is_system, is_reparse = _windows_attributes(stat_result)
    is_hidden = windows_hidden or name.startswith(".")
    readonly_attr = int(getattr(stat_module, "FILE_ATTRIBUTE_READONLY", 0) or 0)
    attributes = int(getattr(stat_result, "st_file_attributes", 0) or 0)
    mode_readonly = not bool(stat_result.st_mode & stat_module.S_IWUSR)
    is_readonly = bool(attributes & readonly_attr) or mode_readonly
    return is_hidden, is_system, is_readonly, is_reparse


# 상대경로가 사용자 제외 무늬와 일치하는지 확인한다.
def _matches_pattern(relative_path: str, name: str, pattern: str) -> bool:
    rel = relative_path.replace("\\", "/")
    normalized_pattern = pattern.replace("\\", "/")
    rel_cf = rel.casefold()
    name_cf = name.casefold()
    pattern_cf = normalized_pattern.casefold()

    if pattern_cf.endswith("/**"):
        prefix = pattern_cf[:-3].rstrip("/")
        if rel_cf == prefix or rel_cf.startswith(prefix + "/"):
            return True
    if fnmatch.fnmatchcase(rel_cf, pattern_cf):
        return True
    if fnmatch.fnmatchcase(name_cf, pattern_cf):
        return True
    try:
        return PurePosixPath(rel_cf).match(pattern_cf)
    except ValueError:
        return False


# 항목을 제외할지 판단하고 사유와 일치 무늬를 돌려준다.
def _exclude_reason(
    relative_path: str,
    name: str,
    is_directory: bool,
    config: ScannerConfig,
) -> tuple[str, str] | None:
    if is_directory and config.use_default_excludes:
        if name.casefold() in DEFAULT_EXCLUDE_DIR_NAMES:
            return "default_exclude", name
    for pattern in config.exclude_patterns:
        if _matches_pattern(relative_path, name, pattern):
            return "custom_exclude", pattern
    return None


# 생성 시각과 운영체제별 시각 출처를 정한다.
def _created_timestamp(stat_result: os.stat_result) -> tuple[str, str]:
    birth_time = getattr(stat_result, "st_birthtime", None)
    if birth_time is not None:
        return _format_local_timestamp(float(birth_time)), "birth_time"
    if os.name == "nt":
        return _format_local_timestamp(stat_result.st_ctime), "windows_creation_time"
    return _format_local_timestamp(stat_result.st_ctime), "metadata_change_time"


# 파일명과 속성에서 사람이 확인할 정리 후보 규칙을 찾는다.
def _cleanup_rules(
    file_id: str,
    relative_path: str,
    filename: str,
    extension: str,
    category: str,
    age_days: int,
    old_days: int,
) -> list[tuple[str, str, str, str]]:
    candidates: list[tuple[str, str, str, str]] = []
    filename_cf = filename.casefold()
    stem_cf = Path(filename).stem.casefold()

    if filename.startswith("~$"):
        candidates.append(
            (
                file_id,
                relative_path,
                "office_lock_file",
                "오피스 임시 잠금 파일명으로 보입니다. 열려 있는 문서인지 확인하십시오.",
            )
        )
    if extension in _TEMP_OR_BACKUP_EXTENSIONS:
        candidates.append(
            (
                file_id,
                relative_path,
                "temporary_or_backup_extension",
                f"임시 또는 백업 파일에 흔한 확장자({extension})입니다.",
            )
        )
    if age_days >= old_days:
        candidates.append(
            (
                file_id,
                relative_path,
                "old_file",
                f"마지막 수정 후 {age_days:,}일이 지났습니다.",
            )
        )
    if category == "executable_installer":
        candidates.append(
            (
                file_id,
                relative_path,
                "installer_or_executable",
                "설치파일 또는 실행파일입니다. 업무 문서와 분리할지 확인하십시오.",
            )
        )
    if _COPY_NAME_RE.search(stem_cf) or filename_cf.startswith("copy of "):
        candidates.append(
            (
                file_id,
                relative_path,
                "possible_copy_name",
                "파일명에 복사본으로 추정되는 표현이 있습니다.",
            )
        )
    return candidates


# 경과 시간을 시, 분, 초 형태로 표시한다.
def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# 현재 진행 상태를 한 줄 문구로 만든다.
def format_progress(snapshot: ProgressSnapshot) -> str:
    root_position = min(snapshot.roots_total, snapshot.roots_completed + 1)
    root_text = (
        f"루트 {root_position}/{snapshot.roots_total} {snapshot.current_root_label}"
        if snapshot.current_root_label
        else f"루트 {snapshot.roots_completed}/{snapshot.roots_total}"
    )
    folder_text = snapshot.current_folder or "."
    base = (
        f"[진행] {root_text} | 현재 폴더 {folder_text} | "
        f"완료 폴더 {snapshot.folders_completed:,}/발견 {snapshot.folders_discovered:,} | "
        f"파일 {snapshot.files_scanned:,}개 | {human_size(snapshot.bytes_scanned)} | "
        f"제외 {snapshot.excluded_count:,}개 | 오류 {snapshot.error_count:,}건 | "
        f"경과 {_format_elapsed(snapshot.elapsed_seconds)}"
    )
    if snapshot.phase == "duplicate_hash":
        base += (
            f" | 중복 확인 {snapshot.hash_files_completed:,}/"
            f"{snapshot.hash_candidates_total:,}"
        )
    if snapshot.current_operation:
        base += f" | {snapshot.current_operation}"
    return base


# 진행 문구를 화면 크기와 출력 환경에 맞춰 표시한다.
class _ProgressPrinter:
    # 진행 출력에 필요한 스트림 상태를 준비한다.
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self._line_open = False
        self._previous_text_length = 0

    # 최신 진행 상태를 한 줄로 출력한다.
    def write(self, snapshot: ProgressSnapshot) -> None:
        text = format_progress(snapshot)
        if self.is_tty:
            trailing_spaces = " " * max(0, self._previous_text_length - len(text))
            self.stream.write("\r" + text + trailing_spaces)
            self.stream.flush()
            self._line_open = True
            self._previous_text_length = len(text)
        else:
            self.stream.write(text + "\n")
            self.stream.flush()

    # 진행 줄이 열려 있으면 다음 출력 전 줄을 마친다.
    def finish_line(self) -> None:
        if self.is_tty and self._line_open:
            self.stream.write("\n")
            self.stream.flush()
            self._line_open = False


# 스캔 결과를 즉시 기록할 데이터베이스 연결과 구조를 관리한다.
class _SQLiteStore:
    # 새 데이터베이스 연결을 만들고 기본 스키마를 구성한다.
    def __init__(self, output_db: Path, config: ScannerConfig) -> None:
        output_db.parent.mkdir(parents=True, exist_ok=True)
        self.output_db = output_db
        self.config = config
        self.connection = sqlite3.connect(str(output_db), timeout=30.0)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = DELETE")
            self.connection.execute("PRAGMA synchronous = NORMAL")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self.connection.execute("PRAGMA temp_store = FILE")
            self.pending_writes = 0
            self._create_schema()
            self._insert_run()
            self._insert_embedded_documents()
            self.connection.commit()
        except BaseException:
            self.connection.close()
            raise

    # 데이터베이스 표, 색인, 조회용 보기를 만든다.
    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE scan_run (
                run_id INTEGER PRIMARY KEY CHECK (run_id = 1),
                schema_version INTEGER NOT NULL,
                scanner_version TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                elapsed_seconds REAL,
                roots_total INTEGER NOT NULL,
                roots_completed INTEGER NOT NULL DEFAULT 0,
                current_root_id INTEGER,
                current_root_label TEXT NOT NULL DEFAULT '',
                current_folder TEXT NOT NULL DEFAULT '',
                current_operation TEXT NOT NULL DEFAULT '',
                last_heartbeat_at TEXT NOT NULL,
                folders_discovered INTEGER NOT NULL DEFAULT 0,
                folders_completed INTEGER NOT NULL DEFAULT 0,
                files_scanned INTEGER NOT NULL DEFAULT 0,
                bytes_scanned INTEGER NOT NULL DEFAULT 0,
                excluded_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                cleanup_rule_hits INTEGER NOT NULL DEFAULT 0,
                hash_candidates_total INTEGER NOT NULL DEFAULT 0,
                hash_files_completed INTEGER NOT NULL DEFAULT 0,
                hash_mode TEXT NOT NULL,
                max_hash_size_bytes INTEGER NOT NULL,
                old_days INTEGER NOT NULL,
                large_count INTEGER NOT NULL,
                use_default_excludes INTEGER NOT NULL,
                exclude_patterns_json TEXT NOT NULL,
                failure_type TEXT,
                failure_message TEXT,
                failure_traceback TEXT
            );

            CREATE TABLE scan_roots (
                root_id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_order INTEGER NOT NULL UNIQUE,
                root_label TEXT NOT NULL UNIQUE,
                root_key TEXT NOT NULL UNIQUE,
                root_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                file_count INTEGER NOT NULL DEFAULT 0,
                folder_count INTEGER NOT NULL DEFAULT 0,
                byte_count INTEGER NOT NULL DEFAULT 0,
                excluded_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE folders (
                folder_id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_id INTEGER NOT NULL REFERENCES scan_roots(root_id),
                parent_folder_id INTEGER REFERENCES folders(folder_id),
                relative_path TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                folder_name TEXT NOT NULL,
                depth INTEGER NOT NULL,
                scan_status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                entry_count INTEGER NOT NULL DEFAULT 0,
                direct_file_count INTEGER NOT NULL DEFAULT 0,
                direct_folder_count INTEGER NOT NULL DEFAULT 0,
                direct_size_bytes INTEGER NOT NULL DEFAULT 0,
                recursive_file_count INTEGER NOT NULL DEFAULT 0,
                recursive_folder_count INTEGER NOT NULL DEFAULT 0,
                recursive_size_bytes INTEGER NOT NULL DEFAULT 0,
                latest_modified_at TEXT NOT NULL DEFAULT '',
                recursive_latest_modified_at TEXT NOT NULL DEFAULT '',
                is_physically_empty INTEGER NOT NULL DEFAULT 0,
                is_hidden INTEGER NOT NULL DEFAULT 0,
                is_system INTEGER NOT NULL DEFAULT 0,
                is_readonly INTEGER NOT NULL DEFAULT 0,
                is_reparse_point INTEGER NOT NULL DEFAULT 0,
                scan_error TEXT,
                UNIQUE(root_id, relative_path)
            );

            CREATE TABLE folder_closure (
                ancestor_folder_id INTEGER NOT NULL REFERENCES folders(folder_id) ON DELETE CASCADE,
                descendant_folder_id INTEGER NOT NULL REFERENCES folders(folder_id) ON DELETE CASCADE,
                depth INTEGER NOT NULL,
                PRIMARY KEY (ancestor_folder_id, descendant_folder_id)
            );

            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                root_id INTEGER NOT NULL REFERENCES scan_roots(root_id),
                folder_id INTEGER NOT NULL REFERENCES folders(folder_id),
                relative_path TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                category TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                created_time_source TEXT NOT NULL,
                modified_at TEXT NOT NULL,
                age_days INTEGER NOT NULL,
                is_hidden INTEGER NOT NULL,
                is_system INTEGER NOT NULL,
                is_readonly INTEGER NOT NULL,
                is_reparse_point INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                hash_status TEXT NOT NULL DEFAULT 'not_requested',
                sha256 TEXT,
                duplicate_group_id TEXT,
                discovered_at TEXT NOT NULL,
                UNIQUE(root_id, relative_path)
            );

            CREATE TABLE cleanup_candidates (
                candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                rule TEXT NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE excluded_paths (
                excluded_id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_id INTEGER NOT NULL REFERENCES scan_roots(root_id),
                relative_path TEXT NOT NULL,
                item_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                matched_pattern TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL
            );

            CREATE TABLE scan_errors (
                error_id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_id INTEGER REFERENCES scan_roots(root_id),
                relative_path TEXT NOT NULL,
                operation TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );

            CREATE TABLE scan_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                level TEXT NOT NULL,
                phase TEXT NOT NULL,
                event_type TEXT NOT NULL,
                root_id INTEGER REFERENCES scan_roots(root_id),
                relative_path TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE duplicate_groups (
                duplicate_group_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                member_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                reclaimable_bytes INTEGER NOT NULL
            );

            CREATE TABLE embedded_documents (
                name TEXT PRIMARY KEY,
                content TEXT NOT NULL
            );

            CREATE INDEX idx_folders_root_status ON folders(root_id, scan_status, folder_id);
            CREATE INDEX idx_folders_parent ON folders(parent_folder_id);
            CREATE INDEX idx_closure_descendant ON folder_closure(descendant_folder_id);
            CREATE INDEX idx_files_root_path ON files(root_id, relative_path);
            CREATE INDEX idx_files_folder ON files(folder_id);
            CREATE INDEX idx_files_size ON files(size_bytes);
            CREATE INDEX idx_files_hash ON files(size_bytes, sha256);
            CREATE INDEX idx_cleanup_file ON cleanup_candidates(file_id);
            CREATE INDEX idx_errors_root ON scan_errors(root_id, error_id);
            CREATE INDEX idx_events_root ON scan_events(root_id, event_id);

            CREATE VIEW v_file_manifest AS
            SELECT
                r.root_label,
                r.root_name,
                f.file_id,
                f.relative_path,
                f.parent_path,
                f.filename,
                f.extension,
                f.category,
                f.size_bytes,
                f.created_at,
                f.created_time_source,
                f.modified_at,
                f.age_days,
                f.is_hidden,
                f.is_system,
                f.is_readonly,
                f.is_reparse_point,
                f.hash_status,
                f.sha256,
                f.duplicate_group_id,
                f.discovered_at
            FROM files AS f
            JOIN scan_roots AS r ON r.root_id = f.root_id;

            CREATE VIEW v_folder_summary AS
            SELECT
                r.root_label,
                r.root_name,
                f.relative_path,
                f.parent_path,
                f.folder_name,
                f.depth,
                f.scan_status,
                f.entry_count,
                f.direct_file_count,
                f.recursive_file_count,
                f.direct_folder_count,
                f.recursive_folder_count,
                f.direct_size_bytes,
                f.recursive_size_bytes,
                f.latest_modified_at,
                f.recursive_latest_modified_at,
                f.is_physically_empty,
                f.is_hidden,
                f.is_system,
                f.is_readonly,
                f.is_reparse_point,
                f.scan_error
            FROM folders AS f
            JOIN scan_roots AS r ON r.root_id = f.root_id;

            CREATE VIEW v_empty_folders AS
            SELECT * FROM v_folder_summary
            WHERE relative_path <> '.' AND is_physically_empty = 1;

            CREATE VIEW v_old_files AS
            SELECT * FROM v_file_manifest
            WHERE age_days >= (SELECT old_days FROM scan_run WHERE run_id = 1);

            CREATE VIEW v_large_files AS
            SELECT * FROM (
                SELECT
                    v_file_manifest.*,
                    ROW_NUMBER() OVER (
                        ORDER BY size_bytes DESC, root_label, relative_path
                    ) AS size_rank
                FROM v_file_manifest
            )
            WHERE size_rank <= (SELECT large_count FROM scan_run WHERE run_id = 1);

            CREATE VIEW v_same_size_candidates AS
            WITH size_groups AS (
                SELECT
                    size_bytes,
                    COUNT(*) AS member_count,
                    DENSE_RANK() OVER (ORDER BY size_bytes DESC) AS group_rank
                FROM files
                WHERE size_bytes > 0
                GROUP BY size_bytes
                HAVING COUNT(*) >= 2
            )
            SELECT
                printf('SIZE-%06d', g.group_rank) AS candidate_group_id,
                g.member_count,
                f.size_bytes,
                r.root_label,
                r.root_name,
                f.file_id,
                f.relative_path,
                f.hash_status,
                f.sha256,
                f.duplicate_group_id,
                CASE
                    WHEN f.duplicate_group_id IS NOT NULL THEN 'confirmed_duplicate'
                    WHEN f.hash_status = 'not_requested' THEN 'unconfirmed_hash_disabled'
                    WHEN f.hash_status = 'skipped_size_limit' THEN 'unconfirmed_size_limit'
                    WHEN f.hash_status = 'skipped_reparse_point' THEN 'skipped_reparse_point'
                    WHEN f.hash_status = 'hashed' THEN 'same_size_but_not_confirmed'
                    ELSE f.hash_status
                END AS confirmation_status
            FROM size_groups AS g
            JOIN files AS f ON f.size_bytes = g.size_bytes
            JOIN scan_roots AS r ON r.root_id = f.root_id;

            CREATE VIEW v_confirmed_duplicates AS
            SELECT
                g.duplicate_group_id,
                g.sha256,
                g.member_count,
                g.size_bytes,
                g.total_bytes,
                g.reclaimable_bytes,
                r.root_label,
                r.root_name,
                f.file_id,
                f.relative_path
            FROM duplicate_groups AS g
            JOIN files AS f ON f.duplicate_group_id = g.duplicate_group_id
            JOIN scan_roots AS r ON r.root_id = f.root_id;

            CREATE VIEW v_category_summary AS
            SELECT category, COUNT(*) AS file_count, SUM(size_bytes) AS total_bytes
            FROM files GROUP BY category;

            CREATE VIEW v_extension_summary AS
            SELECT extension, COUNT(*) AS file_count, SUM(size_bytes) AS total_bytes
            FROM files GROUP BY extension;

            PRAGMA user_version = 2;
            """
        )

    # 실행 설정과 초기 상태를 한 행으로 기록한다.
    def _insert_run(self) -> None:
        now = _now_text()
        self.connection.execute(
            """
            INSERT INTO scan_run (
                run_id, schema_version, scanner_version, status, phase,
                started_at, last_heartbeat_at, roots_total, hash_mode,
                max_hash_size_bytes, old_days, large_count,
                use_default_excludes, exclude_patterns_json
            ) VALUES (1, ?, ?, 'running', 'initializing', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                DATABASE_SCHEMA_VERSION,
                SCANNER_VERSION,
                now,
                now,
                len(self.config.roots),
                self.config.hash_mode,
                self.config.max_hash_size_bytes,
                self.config.old_days,
                self.config.large_count,
                int(self.config.use_default_excludes),
                json.dumps(self.config.exclude_patterns, ensure_ascii=False),
            ),
        )

    # 데이터베이스 사용법과 인공지능 분석 지시문을 내부 문서로 넣는다.
    def _insert_embedded_documents(self) -> None:
        self.connection.executemany(
            "INSERT INTO embedded_documents(name, content) VALUES (?, ?)",
            (
                ("DATABASE_GUIDE", _database_guide_text()),
                ("AI_ANALYSIS_PROMPT", _ai_prompt_text()),
            ),
        )

    # 쓰기 작업 수를 세고 필요한 시점에 변경 내용을 확정한다.
    def _count_write(self, count: int = 1) -> None:
        self.pending_writes += count
        if self.pending_writes >= self.config.commit_every:
            self.commit()

    # 현재 변경 내용을 데이터베이스 파일에 확정한다.
    def commit(self) -> None:
        self.connection.commit()
        self.pending_writes = 0

    # 진행 상태를 실행 상태 표에 갱신한다.
    def update_progress(self, snapshot: ProgressSnapshot, force_commit: bool = False) -> None:
        self.connection.execute(
            """
            UPDATE scan_run SET
                phase = ?, roots_completed = ?, current_root_id = ?,
                current_root_label = ?, current_folder = ?, current_operation = ?,
                last_heartbeat_at = ?, folders_discovered = ?, folders_completed = ?,
                files_scanned = ?, bytes_scanned = ?, excluded_count = ?,
                error_count = ?, cleanup_rule_hits = ?, hash_candidates_total = ?,
                hash_files_completed = ?, elapsed_seconds = ?
            WHERE run_id = 1
            """,
            (
                snapshot.phase,
                snapshot.roots_completed,
                snapshot.current_root_id,
                snapshot.current_root_label,
                snapshot.current_folder,
                snapshot.current_operation,
                _now_text(),
                snapshot.folders_discovered,
                snapshot.folders_completed,
                snapshot.files_scanned,
                snapshot.bytes_scanned,
                snapshot.excluded_count,
                snapshot.error_count,
                snapshot.cleanup_rule_hits,
                snapshot.hash_candidates_total,
                snapshot.hash_files_completed,
                snapshot.elapsed_seconds,
            ),
        )
        self._count_write()
        if force_commit:
            self.commit()

    # 새 스캔 루트와 루트 폴더를 데이터베이스에 등록한다.
    def insert_root(self, order: int, root: Path) -> tuple[int, int, str, str]:
        label = f"ROOT-{order:03d}"
        root_key = stable_root_key(root)
        now = _now_text()
        cursor = self.connection.execute(
            """
            INSERT INTO scan_roots(
                root_order, root_label, root_key, root_name, status, started_at
            ) VALUES (?, ?, ?, ?, 'queued', ?)
            """,
            (order, label, root_key, root.name or root.anchor, now),
        )
        root_id = int(cursor.lastrowid)
        try:
            stat_result = root.stat()
            is_hidden, is_system, is_readonly, is_reparse = _item_flags(
                root.name or root.anchor, stat_result
            )
            modified_at = _format_local_timestamp(stat_result.st_mtime)
        except OSError:
            is_hidden = is_system = is_readonly = is_reparse = False
            modified_at = ""
        folder_id = self.insert_folder(
            root_id=root_id,
            parent_folder_id=None,
            relative_path=".",
            folder_name=root.name or root.anchor,
            depth=0,
            is_hidden=is_hidden,
            is_system=is_system,
            is_readonly=is_readonly,
            is_reparse_point=is_reparse,
            latest_modified_at=modified_at,
        )
        self._count_write()
        return root_id, folder_id, label, root_key

    # 새 폴더와 조상 관계를 즉시 데이터베이스에 기록한다.
    def insert_folder(
        self,
        *,
        root_id: int,
        parent_folder_id: int | None,
        relative_path: str,
        folder_name: str,
        depth: int,
        is_hidden: bool,
        is_system: bool,
        is_readonly: bool,
        is_reparse_point: bool,
        latest_modified_at: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO folders(
                root_id, parent_folder_id, relative_path, parent_path,
                folder_name, depth, scan_status, latest_modified_at,
                is_hidden, is_system, is_readonly, is_reparse_point
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                parent_folder_id,
                relative_path,
                _parent_relative(relative_path),
                folder_name,
                depth,
                latest_modified_at,
                int(is_hidden),
                int(is_system),
                int(is_readonly),
                int(is_reparse_point),
            ),
        )
        folder_id = int(cursor.lastrowid)
        if parent_folder_id is not None:
            self.connection.execute(
                """
                INSERT INTO folder_closure(ancestor_folder_id, descendant_folder_id, depth)
                SELECT ancestor_folder_id, ?, depth + 1
                FROM folder_closure
                WHERE descendant_folder_id = ?
                """,
                (folder_id, parent_folder_id),
            )
        self.connection.execute(
            """
            INSERT INTO folder_closure(ancestor_folder_id, descendant_folder_id, depth)
            VALUES (?, ?, 0)
            """,
            (folder_id, folder_id),
        )
        self._count_write(3 if parent_folder_id is not None else 2)
        return folder_id

    # 다음에 처리할 대기 폴더를 한 건 읽는다.
    def next_queued_folder(self, root_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT folder_id, relative_path, latest_modified_at
            FROM folders
            WHERE root_id = ? AND scan_status = 'queued'
            ORDER BY folder_id
            LIMIT 1
            """,
            (root_id,),
        ).fetchone()

    # 폴더를 처리 중 상태로 바꾸고 시작 시각을 남긴다.
    def mark_folder_scanning(self, folder_id: int) -> None:
        self.connection.execute(
            "UPDATE folders SET scan_status = 'scanning', started_at = ? WHERE folder_id = ?",
            (_now_text(), folder_id),
        )
        self._count_write()

    # 폴더의 직접 집계값과 처리 결과를 확정한다.
    def finish_folder(
        self,
        *,
        folder_id: int,
        status: str,
        entry_count: int,
        direct_file_count: int,
        direct_folder_count: int,
        direct_size_bytes: int,
        latest_modified_at: str,
        is_physically_empty: bool,
        scan_error: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE folders SET
                scan_status = ?, finished_at = ?, entry_count = ?,
                direct_file_count = ?, direct_folder_count = ?,
                direct_size_bytes = ?, latest_modified_at = ?,
                is_physically_empty = ?, scan_error = ?
            WHERE folder_id = ?
            """,
            (
                status,
                _now_text(),
                entry_count,
                direct_file_count,
                direct_folder_count,
                direct_size_bytes,
                latest_modified_at,
                int(is_physically_empty),
                scan_error,
                folder_id,
            ),
        )
        self._count_write()

    # 발견한 파일 한 건을 즉시 데이터베이스에 기록한다.
    def insert_file(self, values: tuple[Any, ...]) -> None:
        self.connection.execute(
            """
            INSERT INTO files(
                file_id, root_id, folder_id, relative_path, parent_path,
                filename, extension, category, size_bytes, created_at,
                created_time_source, modified_at, age_days, is_hidden,
                is_system, is_readonly, is_reparse_point, mtime_ns,
                hash_status, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        self._count_write()

    # 정리 검토 후보를 파일 발견 직후 기록한다.
    def insert_cleanup_candidates(self, rows: list[tuple[str, str, str, str]]) -> None:
        if not rows:
            return
        self.connection.executemany(
            """
            INSERT INTO cleanup_candidates(file_id, relative_path, rule, reason)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        self._count_write(len(rows))

    # 제외한 경로와 제외 사유를 즉시 기록한다.
    def insert_excluded(
        self,
        root_id: int,
        relative_path: str,
        item_type: str,
        reason: str,
        matched_pattern: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO excluded_paths(
                root_id, relative_path, item_type, reason, matched_pattern, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (root_id, relative_path, item_type, reason, matched_pattern, _now_text()),
        )
        self._count_write()

    # 조회 오류의 위치와 원인을 즉시 기록한다.
    def insert_error(
        self,
        root_id: int | None,
        relative_path: str,
        operation: str,
        exc: BaseException,
        message: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO scan_errors(
                root_id, relative_path, operation, error_type, message, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                relative_path,
                operation,
                type(exc).__name__,
                message if message is not None else str(exc),
                _now_text(),
            ),
        )
        self._count_write()

    # 주요 단계와 폴더 처리 이력을 사건 기록으로 남긴다.
    def insert_event(
        self,
        *,
        level: str,
        phase: str,
        event_type: str,
        root_id: int | None = None,
        relative_path: str = "",
        message: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO scan_events(
                occurred_at, level, phase, event_type, root_id, relative_path, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_now_text(), level, phase, event_type, root_id, relative_path, message),
        )
        self._count_write()

    # 루트별 처리 상태와 집계값을 현재 데이터로 갱신한다.
    def finish_root(self, root_id: int, status: str) -> None:
        self.connection.execute(
            """
            UPDATE scan_roots SET
                status = ?, finished_at = ?,
                file_count = (SELECT COUNT(*) FROM files WHERE root_id = ?),
                folder_count = (SELECT COUNT(*) FROM folders WHERE root_id = ?),
                byte_count = COALESCE((SELECT SUM(size_bytes) FROM files WHERE root_id = ?), 0),
                excluded_count = (SELECT COUNT(*) FROM excluded_paths WHERE root_id = ?),
                error_count = (SELECT COUNT(*) FROM scan_errors WHERE root_id = ?)
            WHERE root_id = ?
            """,
            (status, _now_text(), root_id, root_id, root_id, root_id, root_id, root_id),
        )
        self._count_write()

    # 폴더 계층 전체의 하위 파일 수와 용량을 집계해 저장한다.
    def materialize_folder_rollups(self) -> None:
        self.connection.executescript(
            """
            DROP TABLE IF EXISTS temp_folder_rollup;
            CREATE TEMP TABLE temp_folder_rollup (
                folder_id INTEGER PRIMARY KEY,
                recursive_file_count INTEGER NOT NULL,
                recursive_folder_count INTEGER NOT NULL,
                recursive_size_bytes INTEGER NOT NULL,
                recursive_latest_modified_at TEXT NOT NULL
            );

            INSERT INTO temp_folder_rollup(
                folder_id, recursive_file_count, recursive_folder_count,
                recursive_size_bytes, recursive_latest_modified_at
            )
            SELECT
                c.ancestor_folder_id,
                COALESCE(SUM(d.direct_file_count), 0),
                COUNT(*) - 1,
                COALESCE(SUM(d.direct_size_bytes), 0),
                COALESCE(MAX(d.latest_modified_at), '')
            FROM folder_closure AS c
            JOIN folders AS d ON d.folder_id = c.descendant_folder_id
            GROUP BY c.ancestor_folder_id;

            UPDATE folders SET
                recursive_file_count = COALESCE((
                    SELECT recursive_file_count FROM temp_folder_rollup
                    WHERE folder_id = folders.folder_id
                ), 0),
                recursive_folder_count = COALESCE((
                    SELECT recursive_folder_count FROM temp_folder_rollup
                    WHERE folder_id = folders.folder_id
                ), 0),
                recursive_size_bytes = COALESCE((
                    SELECT recursive_size_bytes FROM temp_folder_rollup
                    WHERE folder_id = folders.folder_id
                ), 0),
                recursive_latest_modified_at = COALESCE((
                    SELECT recursive_latest_modified_at FROM temp_folder_rollup
                    WHERE folder_id = folders.folder_id
                ), '');

            DROP TABLE temp_folder_rollup;
            """
        )
        self._count_write()

    # 같은 크기의 해시 후보 수를 계산한다.
    def count_hash_candidates(self) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM files AS f
                JOIN (
                    SELECT size_bytes FROM files
                    WHERE size_bytes > 0
                    GROUP BY size_bytes HAVING COUNT(*) >= 2
                ) AS g ON g.size_bytes = f.size_bytes
                """
            ).fetchone()[0]
        )

    # 중복 확인 대상 파일을 한 행씩 읽는 커서를 만든다.
    def iter_hash_candidates(self) -> sqlite3.Cursor:
        self.connection.execute(
            "UPDATE files SET hash_status = 'zero_length_not_hashed' WHERE size_bytes = 0"
        )
        self.connection.execute(
            """
            UPDATE files SET hash_status = 'not_size_candidate'
            WHERE size_bytes > 0 AND size_bytes NOT IN (
                SELECT size_bytes FROM files
                WHERE size_bytes > 0
                GROUP BY size_bytes HAVING COUNT(*) >= 2
            )
            """
        )
        self._count_write(2)
        self.commit()
        return self.connection.execute(
            """
            SELECT
                f.file_id, f.root_id, r.root_label, f.relative_path,
                f.parent_path, f.size_bytes, f.mtime_ns, f.is_reparse_point
            FROM files AS f
            JOIN scan_roots AS r ON r.root_id = f.root_id
            JOIN (
                SELECT size_bytes FROM files
                WHERE size_bytes > 0
                GROUP BY size_bytes HAVING COUNT(*) >= 2
            ) AS g ON g.size_bytes = f.size_bytes
            ORDER BY f.size_bytes DESC, f.root_id, f.relative_path
            """
        )

    # 파일의 해시 결과와 처리 상태를 즉시 갱신한다.
    def update_file_hash(self, file_id: str, status: str, digest: str | None) -> None:
        self.connection.execute(
            "UPDATE files SET hash_status = ?, sha256 = ? WHERE file_id = ?",
            (status, digest, file_id),
        )
        self._count_write()

    # 확인된 해시 묶음을 중복 그룹 표와 파일 표에 반영한다.
    def build_duplicate_groups(self) -> int:
        self.connection.execute("DELETE FROM duplicate_groups")
        self.connection.execute("UPDATE files SET duplicate_group_id = NULL")
        rows = self.connection.execute(
            """
            SELECT size_bytes, sha256, COUNT(*) AS member_count
            FROM files
            WHERE sha256 IS NOT NULL AND sha256 <> ''
            GROUP BY size_bytes, sha256
            HAVING COUNT(*) >= 2
            ORDER BY size_bytes DESC, sha256
            """
        )
        group_count = 0
        for index, row in enumerate(rows, start=1):
            group_count = index
            group_id = f"DUP-{index:06d}"
            size = int(row["size_bytes"])
            count = int(row["member_count"])
            self.connection.execute(
                """
                INSERT INTO duplicate_groups(
                    duplicate_group_id, sha256, size_bytes, member_count,
                    total_bytes, reclaimable_bytes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, row["sha256"], size, count, size * count, size * (count - 1)),
            )
            self.connection.execute(
                """
                UPDATE files SET duplicate_group_id = ?
                WHERE size_bytes = ? AND sha256 = ?
                """,
                (group_id, size, row["sha256"]),
            )
            self._count_write(2)
        self.commit()
        return group_count

    # 정상 종료 상태와 최종 진행 수치를 기록한다.
    def mark_completed(self, snapshot: ProgressSnapshot, status: str) -> None:
        self.update_progress(snapshot, force_commit=True)
        self.connection.execute(
            """
            UPDATE scan_run SET
                status = ?, phase = 'completed', finished_at = ?,
                last_heartbeat_at = ?, elapsed_seconds = ?, current_operation = '완료'
            WHERE run_id = 1
            """,
            (status, _now_text(), _now_text(), snapshot.elapsed_seconds),
        )
        self.commit()

    # 데이터베이스에 실제로 남은 행을 기준으로 진행 건수를 다시 맞춘다.
    def reconcile_progress_counts(self, snapshot: ProgressSnapshot) -> None:
        row = self.connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM scan_roots
                 WHERE status IN ('completed', 'completed_with_errors')) AS roots_completed,
                (SELECT COUNT(*) FROM folders) AS folders_discovered,
                (SELECT COUNT(*) FROM folders
                 WHERE scan_status IN ('completed', 'error')) AS folders_completed,
                (SELECT COUNT(*) FROM files) AS files_scanned,
                COALESCE((SELECT SUM(size_bytes) FROM files), 0) AS bytes_scanned,
                (SELECT COUNT(*) FROM excluded_paths) AS excluded_count,
                (SELECT COUNT(*) FROM scan_errors) AS error_count,
                (SELECT COUNT(*) FROM cleanup_candidates) AS cleanup_rule_hits,
                (SELECT COUNT(*) FROM files
                 WHERE hash_status IN (
                     'hashed', 'skipped_size_limit', 'skipped_reparse_point',
                     'error', 'changed_during_hash'
                 )) AS hash_files_completed
            """
        ).fetchone()
        snapshot.roots_completed = int(row["roots_completed"])
        snapshot.folders_discovered = int(row["folders_discovered"])
        snapshot.folders_completed = int(row["folders_completed"])
        snapshot.files_scanned = int(row["files_scanned"])
        snapshot.bytes_scanned = int(row["bytes_scanned"])
        snapshot.excluded_count = int(row["excluded_count"])
        snapshot.error_count = int(row["error_count"])
        snapshot.cleanup_rule_hits = int(row["cleanup_rule_hits"])
        snapshot.hash_files_completed = int(row["hash_files_completed"])

    # 예기치 않은 실패 상태와 예외 정보를 기록한다.
    def mark_failed(
        self,
        snapshot: ProgressSnapshot,
        exc: BaseException,
        traceback_text: str,
        failure_message: str,
        status: str = "failed",
    ) -> None:
        try:
            self.commit()
        except sqlite3.Error:
            self.connection.rollback()
            self.pending_writes = 0
        self.reconcile_progress_counts(snapshot)
        self.update_progress(snapshot, force_commit=False)
        finished_at = _now_text()
        if snapshot.current_root_id is not None:
            self.connection.execute(
                """
                UPDATE scan_roots SET
                    status = ?, finished_at = ?,
                    file_count = (SELECT COUNT(*) FROM files WHERE root_id = ?),
                    folder_count = (SELECT COUNT(*) FROM folders WHERE root_id = ?),
                    byte_count = COALESCE(
                        (SELECT SUM(size_bytes) FROM files WHERE root_id = ?), 0
                    ),
                    excluded_count = (
                        SELECT COUNT(*) FROM excluded_paths WHERE root_id = ?
                    ),
                    error_count = (SELECT COUNT(*) FROM scan_errors WHERE root_id = ?)
                WHERE root_id = ? AND status = 'running'
                """,
                (
                    status,
                    finished_at,
                    snapshot.current_root_id,
                    snapshot.current_root_id,
                    snapshot.current_root_id,
                    snapshot.current_root_id,
                    snapshot.current_root_id,
                    snapshot.current_root_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE folders SET scan_status = ?, finished_at = ?,
                    scan_error = COALESCE(scan_error, ?)
                WHERE root_id = ? AND scan_status = 'scanning'
                """,
                (status, finished_at, failure_message, snapshot.current_root_id),
            )
        self.connection.execute(
            """
            INSERT INTO scan_events(
                occurred_at, level, phase, event_type, root_id, relative_path, message
            ) VALUES (?, 'error', ?, ?, ?, ?, ?)
            """,
            (
                finished_at,
                status,
                "scan_interrupted" if status == "interrupted" else "scan_failed",
                snapshot.current_root_id,
                snapshot.current_folder,
                failure_message,
            ),
        )
        self.connection.execute(
            """
            UPDATE scan_run SET
                status = ?, phase = ?, finished_at = ?,
                last_heartbeat_at = ?, elapsed_seconds = ?,
                failure_type = ?, failure_message = ?, failure_traceback = ?
            WHERE run_id = 1
            """,
            (
                status,
                status,
                finished_at,
                finished_at,
                snapshot.elapsed_seconds,
                type(exc).__name__,
                failure_message,
                traceback_text,
            ),
        )
        self.commit()

    # 데이터베이스 연결을 안전하게 닫는다.
    def close(self) -> None:
        try:
            self.connection.commit()
        finally:
            self.connection.close()


# 화면 출력과 데이터베이스 진행 상태 갱신 시점을 관리한다.
class _ProgressController:
    # 진행 상태와 출력 조건을 초기화한다.
    def __init__(
        self,
        *,
        store: _SQLiteStore,
        config: ScannerConfig,
        stream: TextIO,
        callback: Callable[[ProgressSnapshot], None] | None,
        started_monotonic: float,
    ) -> None:
        self.store = store
        self.config = config
        self.printer = _ProgressPrinter(stream)
        self.callback = callback
        self.started_monotonic = started_monotonic
        self.snapshot = ProgressSnapshot(phase="initializing", roots_total=len(config.roots))
        self.last_print_monotonic = 0.0
        self.last_print_file_count = 0

    # 현재 경과 시간을 진행 상태에 반영한다.
    def _refresh_elapsed(self) -> None:
        self.snapshot.elapsed_seconds = time.monotonic() - self.started_monotonic

    # 진행 상태를 데이터베이스에 확정하고 필요한 경우 화면과 호출자에게 알린다.
    def publish(self, force: bool = False) -> None:
        self._refresh_elapsed()
        now = time.monotonic()
        due_time = (
            self.config.progress_interval_seconds == 0
            or now - self.last_print_monotonic >= self.config.progress_interval_seconds
        )
        due_files = (
            self.config.progress_every_files > 0
            and self.snapshot.files_scanned - self.last_print_file_count
            >= self.config.progress_every_files
        )
        should_emit = force or due_time or due_files
        self.store.update_progress(self.snapshot, force_commit=should_emit)
        if not should_emit:
            return
        self.printer.write(self.snapshot)
        if self.callback is not None:
            self.callback(replace(self.snapshot))
        self.last_print_monotonic = now
        self.last_print_file_count = self.snapshot.files_scanned

    # 현재 단계와 작업 내용을 바꾸고 즉시 알린다.
    def set_phase(self, phase: str, operation: str) -> None:
        self.snapshot.phase = phase
        self.snapshot.current_operation = operation
        self.publish(force=True)

    # 현재 처리 중인 루트와 폴더를 바꾸고 즉시 알린다.
    def enter_folder(
        self, root_id: int, root_label: str, relative_path: str, operation: str
    ) -> None:
        self.snapshot.current_root_id = root_id
        self.snapshot.current_root_label = root_label
        self.snapshot.current_folder = relative_path
        self.snapshot.current_operation = operation
        self.publish(force=True)

    # 새 폴더를 발견한 누적 건수를 반영한다.
    def discovered_folder(self) -> None:
        self.snapshot.folders_discovered += 1

    # 폴더 처리가 끝난 누적 건수를 반영한다.
    def completed_folder(self) -> None:
        self.snapshot.folders_completed += 1
        self.publish()

    # 새 파일의 개수와 용량을 누적하고 필요하면 진행 상태를 알린다.
    def discovered_file(self, size_bytes: int) -> None:
        self.snapshot.files_scanned += 1
        self.snapshot.bytes_scanned += size_bytes
        self.publish()

    # 제외 항목 수를 누적한다.
    def excluded_item(self) -> None:
        self.snapshot.excluded_count += 1
        self.publish()

    # 조회 오류 수를 누적하고 즉시 알린다.
    def recorded_error(self) -> None:
        self.snapshot.error_count += 1
        self.publish(force=True)

    # 정리 검토 규칙 적중 건수를 누적한다.
    def cleanup_hits(self, count: int) -> None:
        self.snapshot.cleanup_rule_hits += count

    # 한 스캔 루트의 처리가 끝난 수를 반영한다.
    def completed_root(self) -> None:
        self.snapshot.roots_completed += 1
        self.publish(force=True)

    # 해시 후보의 전체 건수를 진행 상태에 반영한다.
    def set_hash_total(self, count: int) -> None:
        self.snapshot.hash_candidates_total = count
        self.snapshot.hash_files_completed = 0
        self.publish(force=True)

    # 해시 확인이 끝난 파일 수를 누적한다.
    def completed_hash_file(self) -> None:
        self.snapshot.hash_files_completed += 1
        self.publish()

    # 진행 출력 줄을 닫아 뒤의 안내 문구가 겹치지 않게 한다.
    def finish_line(self) -> None:
        self.printer.finish_line()


# 오류 메시지와 추적 정보에서 로컬 절대경로를 가린다.
def _redact_paths(text: str, roots: Sequence[Path], output_db: Path) -> str:
    result = text
    replacements: list[tuple[str, str]] = []
    for index, root in enumerate(roots, start=1):
        token = f"<ROOT-{index:03d}>"
        raw = str(root)
        replacements.extend(
            ((raw, token), (raw.replace("\\", "/"), token), (raw.replace("/", "\\"), token))
        )
    output_raw = str(output_db)
    replacements.extend(
        (
            (output_raw, "<OUTPUT_DB>"),
            (output_raw.replace("\\", "/"), "<OUTPUT_DB>"),
            (output_raw.replace("/", "\\"), "<OUTPUT_DB>"),
        )
    )
    for raw, token in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if raw:
            result = result.replace(raw, token)
    return result


# 파일을 읽기 전용으로 읽어 내용 지문과 처리 상태를 계산한다.
def _hash_file_path(
    path: Path,
    expected_size: int,
    expected_mtime_ns: int,
) -> tuple[str, str | None, BaseException | None]:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = path.stat()
    except OSError as exc:
        return "error", None, exc

    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or after.st_size != expected_size
        or after.st_mtime_ns != expected_mtime_ns
    ):
        return (
            "changed_during_hash",
            None,
            RuntimeError("해시 계산 중 파일이 변경되어 결과를 버렸습니다."),
        )
    return "hashed", digest.hexdigest(), None


# 한 루트의 대기 폴더를 차례로 읽어 발견 즉시 데이터베이스에 기록한다.
def _scan_root(
    *,
    context: _RootContext,
    config: ScannerConfig,
    store: _SQLiteStore,
    progress: _ProgressController,
    roots: Sequence[Path],
    output_db: Path,
) -> None:
    store.connection.execute(
        "UPDATE scan_roots SET status = 'running', started_at = ? WHERE root_id = ?",
        (_now_text(), context.root_id),
    )
    store.insert_event(
        level="info",
        phase="metadata",
        event_type="root_started",
        root_id=context.root_id,
        message=context.root_label,
    )
    store.commit()
    now_epoch = time.time()

    while True:
        queued = store.next_queued_folder(context.root_id)
        if queued is None:
            break
        folder_id = int(queued["folder_id"])
        relative_folder = str(queued["relative_path"])
        stored_folder_modified = str(queued["latest_modified_at"] or "")
        absolute_folder = _absolute_from_relative(context.absolute_path, relative_folder)

        progress.enter_folder(
            context.root_id,
            context.root_label,
            relative_folder,
            "폴더 항목 조회",
        )
        store.mark_folder_scanning(folder_id)
        store.insert_event(
            level="info",
            phase="metadata",
            event_type="folder_started",
            root_id=context.root_id,
            relative_path=relative_folder,
        )
        store.commit()

        entry_count = 0
        direct_file_count = 0
        direct_folder_count = 0
        direct_size_bytes = 0
        latest_modified_epoch = 0.0

        try:
            with os.scandir(absolute_folder) as iterator:
                for entry in iterator:
                    entry_count += 1
                    relative = _join_relative(relative_folder, entry.name)
                    try:
                        stat_result = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        safe_message = _redact_paths(str(exc), roots, output_db)
                        store.insert_error(
                            context.root_id,
                            relative,
                            "stat",
                            exc,
                            safe_message,
                        )
                        store.insert_event(
                            level="error",
                            phase="metadata",
                            event_type="entry_stat_failed",
                            root_id=context.root_id,
                            relative_path=relative,
                            message=safe_message,
                        )
                        progress.recorded_error()
                        continue

                    latest_modified_epoch = max(
                        latest_modified_epoch, stat_result.st_mtime
                    )
                    is_hidden, is_system, is_readonly, is_reparse = _item_flags(
                        entry.name, stat_result
                    )
                    try:
                        is_link = entry.is_symlink()
                    except OSError:
                        is_link = is_reparse
                    is_directory = stat_module.S_ISDIR(stat_result.st_mode)

                    if is_link or (is_reparse and is_directory):
                        store.insert_excluded(
                            context.root_id,
                            relative,
                            "link_or_reparse_point",
                            "link_not_followed"
                            if is_link
                            else "directory_reparse_point_not_followed",
                        )
                        progress.excluded_item()
                        continue

                    exclusion = _exclude_reason(
                        relative,
                        entry.name,
                        is_directory,
                        config,
                    )
                    if exclusion is not None:
                        reason, pattern = exclusion
                        store.insert_excluded(
                            context.root_id,
                            relative,
                            "directory" if is_directory else "file",
                            reason,
                            pattern,
                        )
                        progress.excluded_item()
                        continue

                    if is_directory:
                        depth = len(PurePosixPath(relative).parts)
                        store.insert_folder(
                            root_id=context.root_id,
                            parent_folder_id=folder_id,
                            relative_path=relative,
                            folder_name=entry.name,
                            depth=depth,
                            is_hidden=is_hidden,
                            is_system=is_system,
                            is_readonly=is_readonly,
                            is_reparse_point=is_reparse,
                            latest_modified_at=_format_local_timestamp(stat_result.st_mtime),
                        )
                        direct_folder_count += 1
                        progress.discovered_folder()
                        continue

                    if not stat_module.S_ISREG(stat_result.st_mode):
                        store.insert_excluded(
                            context.root_id,
                            relative,
                            "special_file",
                            "non_regular_file_not_scanned",
                        )
                        progress.excluded_item()
                        continue

                    extension = Path(entry.name).suffix.casefold()
                    created_at, created_source = _created_timestamp(stat_result)
                    age_days = max(0, int((now_epoch - stat_result.st_mtime) // 86400))
                    file_id = stable_file_id(context.root_key, relative)
                    hash_status = (
                        "skipped_reparse_point"
                        if config.hash_mode == "duplicates" and is_reparse
                        else "not_requested"
                    )
                    store.insert_file(
                        (
                            file_id,
                            context.root_id,
                            folder_id,
                            relative,
                            relative_folder,
                            entry.name,
                            extension,
                            classify_extension(extension),
                            stat_result.st_size,
                            created_at,
                            created_source,
                            _format_local_timestamp(stat_result.st_mtime),
                            age_days,
                            int(is_hidden),
                            int(is_system),
                            int(is_readonly),
                            int(is_reparse),
                            stat_result.st_mtime_ns,
                            hash_status,
                            _now_text(),
                        )
                    )
                    cleanup_rows = _cleanup_rules(
                        file_id,
                        relative,
                        entry.name,
                        extension,
                        classify_extension(extension),
                        age_days,
                        config.old_days,
                    )
                    store.insert_cleanup_candidates(cleanup_rows)
                    progress.cleanup_hits(len(cleanup_rows))

                    direct_file_count += 1
                    direct_size_bytes += stat_result.st_size
                    progress.discovered_file(stat_result.st_size)
        except OSError as exc:
            safe_message = _redact_paths(str(exc), roots, output_db)
            store.insert_error(
                context.root_id,
                relative_folder,
                "scandir",
                exc,
                safe_message,
            )
            store.finish_folder(
                folder_id=folder_id,
                status="error",
                entry_count=entry_count,
                direct_file_count=direct_file_count,
                direct_folder_count=direct_folder_count,
                direct_size_bytes=direct_size_bytes,
                latest_modified_at=(
                    _format_local_timestamp(latest_modified_epoch)
                    if latest_modified_epoch
                    else stored_folder_modified
                ),
                is_physically_empty=entry_count == 0,
                scan_error=safe_message,
            )
            store.insert_event(
                level="error",
                phase="metadata",
                event_type="folder_failed",
                root_id=context.root_id,
                relative_path=relative_folder,
                message=safe_message,
            )
            progress.recorded_error()
            progress.completed_folder()
            store.commit()
            continue

        store.finish_folder(
            folder_id=folder_id,
            status="completed",
            entry_count=entry_count,
            direct_file_count=direct_file_count,
            direct_folder_count=direct_folder_count,
            direct_size_bytes=direct_size_bytes,
            latest_modified_at=(
                _format_local_timestamp(latest_modified_epoch)
                if latest_modified_epoch
                else stored_folder_modified
            ),
            is_physically_empty=entry_count == 0,
        )
        store.insert_event(
            level="info",
            phase="metadata",
            event_type="folder_completed",
            root_id=context.root_id,
            relative_path=relative_folder,
        )
        progress.completed_folder()
        store.commit()

    root_status = (
        "completed_with_errors"
        if progress.snapshot.error_count > context.error_count_at_start
        else "completed"
    )
    store.finish_root(context.root_id, root_status)
    store.insert_event(
        level="info",
        phase="metadata",
        event_type="root_completed",
        root_id=context.root_id,
        message=root_status,
    )
    progress.completed_root()
    store.commit()


# 같은 크기의 후보 파일을 한 건씩 읽어 중복 여부를 확인한다.
def _hash_duplicate_candidates(
    *,
    contexts: dict[int, _RootContext],
    config: ScannerConfig,
    store: _SQLiteStore,
    progress: _ProgressController,
    roots: Sequence[Path],
    output_db: Path,
) -> int:
    progress.set_phase("duplicate_hash", "중복 후보 수 계산")
    total = store.count_hash_candidates()
    progress.set_hash_total(total)

    for row in store.iter_hash_candidates():
        root_id = int(row["root_id"])
        context = contexts[root_id]
        relative_path = str(row["relative_path"])
        progress.enter_folder(
            root_id,
            str(row["root_label"]),
            str(row["parent_path"]),
            f"중복 확인: {relative_path}",
        )

        if bool(row["is_reparse_point"]):
            store.update_file_hash(str(row["file_id"]), "skipped_reparse_point", None)
            progress.completed_hash_file()
            continue
        if config.max_hash_size_bytes and int(row["size_bytes"]) > config.max_hash_size_bytes:
            store.update_file_hash(str(row["file_id"]), "skipped_size_limit", None)
            progress.completed_hash_file()
            continue

        absolute_path = _absolute_from_relative(context.absolute_path, relative_path)
        status, digest, error = _hash_file_path(
            absolute_path,
            int(row["size_bytes"]),
            int(row["mtime_ns"]),
        )
        store.update_file_hash(str(row["file_id"]), status, digest)
        if error is not None:
            safe_message = _redact_paths(str(error), roots, output_db)
            store.insert_error(
                root_id,
                relative_path,
                "sha256",
                error,
                safe_message,
            )
            progress.recorded_error()
        progress.completed_hash_file()

    store.commit()
    progress.set_phase("duplicate_grouping", "확인된 중복 묶음 생성")
    group_count = store.build_duplicate_groups()
    store.insert_event(
        level="info",
        phase="duplicate_grouping",
        event_type="duplicate_grouping_completed",
        message=f"{group_count} groups",
    )
    store.commit()
    return group_count


# 데이터베이스 안에 넣을 표와 보기의 사용법을 작성한다.
def _database_guide_text() -> str:
    return """이 파일은 조회 전용 스캐너가 만든 SQLite 데이터베이스입니다.

먼저 확인할 항목
1. scan_run: 실행 상태, 마지막 처리 폴더, 누적 건수, 실패 원인
2. scan_roots: 입력한 스캔 루트별 상태와 집계
3. v_folder_summary: 폴더별 직접 및 하위 파일 수와 용량
4. v_file_manifest: 전체 파일 메타데이터
5. cleanup_candidates: 사람이 확인할 정리 후보
6. v_same_size_candidates: 크기가 같은 파일 후보
7. v_confirmed_duplicates: SHA-256까지 같은 파일 묶음
8. scan_errors: 권한, 파일 접근, 해시 오류
9. scan_events: 폴더 시작·완료와 주요 단계 이력

실행 중 중단된 데이터베이스라면 scan_run.status가 running, interrupted 또는 failed로 남습니다. current_root_label, current_folder, current_operation, last_heartbeat_at을 확인하면 마지막으로 처리하던 위치를 알 수 있습니다.

파일 내용은 저장하지 않습니다. 파일명과 폴더명도 업무정보가 될 수 있으므로 외부 인공지능에 전달하기 전에 내부 반출 규정을 확인하십시오.
"""


# 데이터베이스 자료를 인공지능에 분석시킬 때 쓸 지시문을 작성한다.
def _ai_prompt_text() -> str:
    return """당신은 업무 파일 정리 설계자다.

첨부한 SQLite 파일은 실제 문서 내용이 아니라 파일명, 상대경로, 확장자, 용량, 수정일 등의 메타데이터를 담고 있다. 먼저 scan_run과 scan_roots에서 실행이 정상 완료됐는지 확인하라. 이어서 v_folder_summary와 v_file_manifest를 분석하라.

규칙
1. 루트는 root_label로 구분한다.
2. 확신할 수 없는 파일은 REVIEW로 둔다.
3. 삭제를 제안하거나 자동 삭제 대상으로 지정하지 않는다.
4. 허용 작업은 KEEP, MOVE, REVIEW, ARCHIVE, QUARANTINE뿐이다.
5. 파일 식별에는 file_id를 사용한다.
6. 비슷한 파일명이나 같은 크기만으로 중복이라고 단정하지 않는다.
7. 실제 중복 여부는 v_confirmed_duplicates를 확인한다.
8. 보존기간, 민감성, 다른 문서의 참조 가능성을 알 수 없으면 REVIEW로 둔다.
9. 먼저 목표 폴더 구조와 분류 규칙을 제안하고, 사용자가 승인한 뒤 파일별 이동안을 작성한다.

파일별 이동안 열 순서
root_label,file_id,action,target_folder,new_filename,confidence,reason

confidence는 0.00부터 1.00까지의 숫자다. 설명과 결과는 한국어로 작성한다.
"""


# 여러 스캔 폴더를 조사해 발견 즉시 단일 데이터베이스 파일에 기록한다.
def scan_to_database(
    config: ScannerConfig,
    *,
    progress_stream: TextIO | None = None,
    progress_callback: Callable[[ProgressSnapshot], None] | None = None,
) -> ScanSummary:
    roots, output_db = validate_scan_paths(config.roots, config.output_db)
    started_monotonic = time.monotonic()
    stream = progress_stream if progress_stream is not None else sys.stdout
    store: _SQLiteStore | None = None
    progress: _ProgressController | None = None
    duplicate_group_count = 0

    try:
        store = _SQLiteStore(output_db, config)
        progress = _ProgressController(
            store=store,
            config=config,
            stream=stream,
            callback=progress_callback,
            started_monotonic=started_monotonic,
        )

        contexts: dict[int, _RootContext] = {}
        for order, root in enumerate(roots, start=1):
            root_id, _folder_id, label, root_key = store.insert_root(order, root)
            contexts[root_id] = _RootContext(
                root_id=root_id,
                root_label=label,
                root_key=root_key,
                absolute_path=root,
                error_count_at_start=0,
            )
            progress.discovered_folder()
        store.commit()

        progress.set_phase("metadata", "파일과 폴더 메타데이터 조회")
        for context in contexts.values():
            context.error_count_at_start = progress.snapshot.error_count
            _scan_root(
                context=context,
                config=config,
                store=store,
                progress=progress,
                roots=roots,
                output_db=output_db,
            )

        if config.hash_mode == "duplicates":
            duplicate_group_count = _hash_duplicate_candidates(
                contexts=contexts,
                config=config,
                store=store,
                progress=progress,
                roots=roots,
                output_db=output_db,
            )

        progress.set_phase("finalizing", "폴더 하위 집계 계산")
        store.materialize_folder_rollups()
        store.commit()

        progress.snapshot.current_operation = "완료"
        progress.snapshot.current_folder = ""
        progress.snapshot.current_root_label = ""
        progress.snapshot.current_root_id = None
        progress.publish(force=True)
        final_status = (
            "completed_with_errors" if progress.snapshot.error_count else "completed"
        )
        store.mark_completed(progress.snapshot, final_status)
        progress.finish_line()

        return ScanSummary(
            output_db=output_db,
            status=final_status,
            root_count=len(roots),
            folder_count=progress.snapshot.folders_discovered,
            file_count=progress.snapshot.files_scanned,
            byte_count=progress.snapshot.bytes_scanned,
            excluded_count=progress.snapshot.excluded_count,
            error_count=progress.snapshot.error_count,
            duplicate_group_count=duplicate_group_count,
            elapsed_seconds=progress.snapshot.elapsed_seconds,
        )
    except KeyboardInterrupt as exc:
        if progress is not None:
            progress._refresh_elapsed()
            progress.snapshot.current_operation = "사용자 중단"
        if store is not None and progress is not None:
            trace = _redact_paths(traceback.format_exc(), roots, output_db)
            safe_message = _redact_paths(str(exc), roots, output_db)
            try:
                store.mark_failed(
                    progress.snapshot,
                    exc,
                    trace,
                    safe_message,
                    status="interrupted",
                )
            except sqlite3.Error:
                pass
        if progress is not None:
            progress.finish_line()
        raise
    except Exception as exc:
        if progress is not None:
            progress._refresh_elapsed()
        if store is not None and progress is not None:
            trace = _redact_paths(traceback.format_exc(), roots, output_db)
            safe_message = _redact_paths(str(exc), roots, output_db)
            try:
                store.mark_failed(progress.snapshot, exc, trace, safe_message)
            except sqlite3.Error:
                pass
        if progress is not None:
            progress.finish_line()
        raise
    finally:
        if store is not None:
            store.close()


# 명령행 옵션과 도움말을 구성한다.
def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "하나 이상의 업무 폴더를 변경하지 않고 조사해 단일 SQLite 파일에 "
            "발견 즉시 기록합니다."
        )
    )
    parser.add_argument(
        "roots",
        nargs="*",
        help="조회할 최상위 폴더를 하나 이상 지정합니다.",
    )
    parser.add_argument(
        "--root",
        dest="extra_roots",
        action="append",
        default=[],
        metavar="PATH",
        help="스캔 폴더를 반복해서 추가합니다. 배치파일에서 사용합니다.",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="FILE.sqlite3",
        help="새로 만들 SQLite 파일 경로입니다. 모든 스캔 폴더 밖에 지정합니다.",
    )
    parser.add_argument(
        "--hash-duplicates",
        action="store_true",
        help="같은 크기의 후보 파일을 읽기 전용으로 열어 SHA-256을 확인합니다.",
    )
    parser.add_argument(
        "--max-hash-size-mb",
        type=int,
        default=4096,
        help="해시할 파일 한 개의 최대 크기입니다. 0은 제한 없음, 기본값은 4096입니다.",
    )
    parser.add_argument(
        "--old-days",
        type=int,
        default=730,
        help="오래된 파일 후보 기준 일수입니다. 기본값은 730입니다.",
    )
    parser.add_argument(
        "--large-count",
        type=int,
        default=100,
        help="v_large_files에 표시할 상위 파일 수입니다. 기본값은 100입니다.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="제외할 이름 또는 상대경로 무늬입니다. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="기본 기술 폴더 제외 규칙을 해제합니다.",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="진행 정보를 다시 표시할 최대 간격입니다. 0이면 모든 변화마다 표시합니다.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=250,
        metavar="FILES",
        help="파일을 지정 개수만큼 찾을 때마다 진행 정보를 표시합니다. 기본값은 250입니다.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=100,
        metavar="ROWS",
        help="지정한 쓰기 건수마다 데이터베이스에 확정합니다. 기본값은 100입니다.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCANNER_VERSION}",
    )
    return parser


# 명령행 인수를 처리하고 스캔 작업을 실행한다.
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    roots = [*args.roots, *args.extra_roots]
    if not roots:
        parser.error("스캔 폴더를 하나 이상 지정하십시오.")

    try:
        if args.max_hash_size_mb < 0:
            raise ValueError("--max-hash-size-mb는 0 이상이어야 합니다.")
        if args.old_days < 0:
            raise ValueError("--old-days는 0 이상이어야 합니다.")
        if args.large_count < 1:
            raise ValueError("--large-count는 1 이상이어야 합니다.")
        if args.progress_interval < 0:
            raise ValueError("--progress-interval은 0 이상이어야 합니다.")
        if args.progress_every < 0:
            raise ValueError("--progress-every는 0 이상이어야 합니다.")
        if args.commit_every < 1:
            raise ValueError("--commit-every는 1 이상이어야 합니다.")

        config = ScannerConfig(
            roots=tuple(Path(root) for root in roots),
            output_db=Path(args.output),
            hash_mode="duplicates" if args.hash_duplicates else "none",
            max_hash_size_bytes=(
                0 if args.max_hash_size_mb == 0 else args.max_hash_size_mb * 1024**2
            ),
            old_days=args.old_days,
            large_count=args.large_count,
            exclude_patterns=tuple(args.exclude),
            use_default_excludes=not args.no_default_excludes,
            progress_interval_seconds=args.progress_interval,
            progress_every_files=args.progress_every,
            commit_every=args.commit_every,
        )
        resolved_roots, output_db = validate_scan_paths(config.roots, config.output_db)

        print("[안전] 스캔 대상의 파일을 이동·삭제·수정하지 않습니다.")
        for index, root in enumerate(resolved_roots, start=1):
            print(f"[대상 {index}] {root}")
        print(f"[출력] {output_db}")
        if config.hash_mode == "duplicates":
            limit = (
                "제한 없음"
                if config.max_hash_size_bytes == 0
                else human_size(config.max_hash_size_bytes)
            )
            print(f"[모드] 메타데이터 조회 후 중복 후보 해시 확인, 파일당 최대 {limit}")
        else:
            print("[모드] 메타데이터 전용, 파일 내용은 열지 않습니다.")

        summary = scan_to_database(config)
        print(
            f"[완료] 상태 {summary.status} | 루트 {summary.root_count:,}개 | "
            f"폴더 {summary.folder_count:,}개 | 파일 {summary.file_count:,}개 | "
            f"오류 {summary.error_count:,}건"
        )
        print(f"[결과] {summary.output_db}")
        return 0
    except KeyboardInterrupt:
        print("\n[중단] 사용자가 작업을 중단했습니다. SQLite 파일에 마지막 진행 상태를 남겼습니다.")
        return 130
    except Exception as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2


# 이 파일을 직접 실행할 때 명령행 진입점을 호출한다.
if __name__ == "__main__":
    raise SystemExit(main())
