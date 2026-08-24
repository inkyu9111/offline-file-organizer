#!/usr/bin/env python3
"""업무 파일/폴더 조회 전용 스캐너.

핵심 안전 원칙
---------------
* 스캔 대상 아래의 파일/폴더를 생성, 수정, 이동, 이름 변경, 삭제하지 않는다.
* 기본 모드에서는 파일 내용을 열지 않고 메타데이터만 조회한다.
* ``hash_mode='duplicates'``일 때에만 동일 크기 후보 파일을 읽기 전용으로
  열어 SHA-256을 계산한다.
* 보고서 출력 경로가 스캔 대상 내부이면 실행을 거부한다.

Python 3.10 이상, 외부 패키지 없이 동작한다.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import os
import re
import stat as stat_module
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


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


# 스캔에 필요한 설정값을 보관하고 입력 조건을 검증한다.
@dataclass(slots=True)
class ScannerConfig:
    """스캔 설정을 보관한다.

    ``output_base``는 보고서를 쓸 때 사용한다. 보고서가 스캔 대상 안에
    생기지 않도록 스캔 시작 전에도 경로를 검사한다.
    """

    root: Path | str
    output_base: Path | str
    hash_mode: str = "none"
    max_hash_size_bytes: int = 4 * 1024**3
    old_days: int = 730
    large_count: int = 100
    exclude_patterns: tuple[str, ...] = ()
    use_default_excludes: bool = True
    progress_every: int = 500

    # 설정값의 유효성을 확인하고 내부 표현을 정규화한다.
    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.output_base = Path(self.output_base)
        self.exclude_patterns = tuple(pattern for pattern in self.exclude_patterns if pattern)
        if self.hash_mode not in {"none", "duplicates"}:
            raise ValueError("hash_mode must be 'none' or 'duplicates'")
        if self.max_hash_size_bytes < 0:
            raise ValueError("max_hash_size_bytes must be zero or positive")
        if self.old_days < 0:
            raise ValueError("old_days must be zero or positive")
        if self.large_count < 1:
            raise ValueError("large_count must be at least 1")
        if self.progress_every < 0:
            raise ValueError("progress_every must be zero or positive")


# 파일 하나의 경로와 속성, 중복 검사 결과를 담는다.
@dataclass(slots=True)
class FileRecord:
    file_id: str
    relative_path: str
    parent_path: str
    filename: str
    extension: str
    category: str
    size_bytes: int
    size_human: str
    created_at: str
    created_time_source: str
    modified_at: str
    age_days: int
    is_hidden: bool
    is_system: bool
    is_readonly: bool
    is_reparse_point: bool
    sha256: str = ""
    hash_status: str = "not_requested"
    duplicate_group_id: str = ""
    _absolute_path: Path = field(default=Path(), repr=False, compare=False)
    _mtime_ns: int = field(default=0, repr=False, compare=False)


# 폴더별 직접 및 하위 항목 집계 결과를 담는다.
@dataclass(slots=True)
class FolderRecord:
    relative_path: str
    depth: int
    direct_file_count: int
    recursive_file_count: int
    direct_folder_count: int
    recursive_folder_count: int
    direct_size_bytes: int
    recursive_size_bytes: int
    latest_modified_at: str
    is_physically_empty: bool
    is_hidden: bool
    is_system: bool


# 스캔에서 제외한 경로와 제외 사유를 기록한다.
@dataclass(slots=True)
class ExcludedRecord:
    relative_path: str
    item_type: str
    reason: str
    matched_pattern: str = ""


# 스캔 중 발생한 오류의 위치와 내용을 기록한다.
@dataclass(slots=True)
class ScanErrorRecord:
    relative_path: str
    operation: str
    error_type: str
    message: str


# 사람이 검토해야 할 정리 후보와 근거를 담는다.
@dataclass(slots=True)
class CleanupCandidate:
    file_id: str
    relative_path: str
    rule: str
    reason: str


# 내용이 같은 파일 묶음과 절감 가능 용량을 나타낸다.
@dataclass(slots=True)
class DuplicateGroup:
    group_id: str
    sha256: str
    size_bytes: int
    member_count: int
    total_bytes: int
    reclaimable_bytes: int
    file_ids: tuple[str, ...]
    relative_paths: tuple[str, ...]


# 한 차례 스캔에서 얻은 모든 결과와 상태를 묶는다.
@dataclass(slots=True)
class ScanResult:
    files: list[FileRecord]
    folders: list[FolderRecord]
    excluded: list[ExcludedRecord]
    errors: list[ScanErrorRecord]
    cleanup_candidates: list[CleanupCandidate]
    duplicate_groups: list[DuplicateGroup]
    started_at: str
    finished_at: str
    elapsed_seconds: float
    status: str


# 폴더별 파일 수와 용량을 계산하는 임시 누적값을 관리한다.
@dataclass(slots=True)
class _FolderAccumulator:
    relative_path: str
    depth: int
    direct_file_count: int = 0
    recursive_file_count: int = 0
    direct_folder_count: int = 0
    recursive_folder_count: int = 0
    direct_size_bytes: int = 0
    recursive_size_bytes: int = 0
    latest_modified_epoch: float = 0.0
    is_physically_empty: bool = False
    is_hidden: bool = False
    is_system: bool = False


# 상대경로를 바탕으로 재현 가능한 파일 식별자를 만든다.
def stable_file_id(relative_path: str) -> str:
    """상대경로를 대소문자 구분 없이 처리해 항상 같은 파일 식별자를 만든다."""
    normalized = relative_path.replace("\\", "/").strip("/").casefold()
    digest = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"F-{digest[:16].upper()}"


# 확장자를 업무 파일 유형으로 분류한다.
def classify_extension(extension: str) -> str:
    normalized = extension.casefold()
    if not normalized:
        return "no_extension"
    for category, extensions in _EXTENSION_CATEGORIES.items():
        if normalized in extensions:
            return category
    return "other"


# 표 계산 프로그램이 값을 수식으로 해석하지 않도록 보호한다.
def excel_safe(value: str) -> str:
    """CSV를 Excel로 열 때 파일명이 수식으로 처리되지 않게 한다."""
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# 바이트 수를 읽기 쉬운 이진 단위 문자열로 바꾼다.
def human_size(size_bytes: int) -> str:
    if size_bytes < 0:
        raise ValueError("size_bytes must be non-negative")
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(size_bytes)
    for index, unit in enumerate(units):
        if value < 1024.0 or index == len(units) - 1:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")


# 한 경로가 다른 경로의 내부인지 안전하게 판별한다.
def _is_within(child: Path, parent: Path) -> bool:
    child_text = os.path.normcase(str(child))
    parent_text = os.path.normcase(str(parent))
    try:
        return os.path.commonpath([child_text, parent_text]) == parent_text
    except ValueError:
        return False


# 스캔 대상과 보고서 경로를 검증하고 절대경로로 정리한다.
def ensure_output_outside_root(
    root: Path | str, output: Path | str
) -> tuple[Path, Path]:
    root_path = Path(root).expanduser().resolve(strict=False)
    output_path = Path(output).expanduser().resolve(strict=False)
    if not root_path.exists():
        raise FileNotFoundError(f"scan root does not exist: {root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"scan root is not a directory: {root_path}")
    if _is_within(output_path, root_path):
        raise ValueError("output directory must be outside the scan root")
    return root_path, output_path


# 시각 값을 현지 시간의 읽기 쉬운 문자열로 바꾼다.
def _format_local_timestamp(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


# 절대경로를 스캔 기준의 상대경로로 변환한다.
def _relative_path(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return relative or "."


# 상대경로에서 상위 폴더 경로를 구한다.
def _parent_relative(relative_path: str) -> str:
    if relative_path == ".":
        return "."
    parent = PurePosixPath(relative_path).parent.as_posix()
    return "." if parent in {"", "."} else parent


# 운영체제 파일 속성값을 안전하게 읽는다.
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


# 파일 속성에서 숨김, 시스템, 읽기 전용, 재분석 지점을 판별한다.
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


# 항목을 제외해야 하는지 판단하고 사유를 돌려준다.
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


# 생성 시각과 그 시각의 출처를 정한다.
def _created_timestamp(stat_result: os.stat_result) -> tuple[str, str]:
    birth_time = getattr(stat_result, "st_birthtime", None)
    if birth_time is not None:
        return _format_local_timestamp(float(birth_time)), "birth_time"
    if os.name == "nt":
        return _format_local_timestamp(stat_result.st_ctime), "windows_creation_time"
    return _format_local_timestamp(stat_result.st_ctime), "metadata_change_time"


# 예외를 절대경로가 드러나지 않는 오류 기록으로 바꾼다.
def _record_error(
    errors: list[ScanErrorRecord],
    relative_path: str,
    operation: str,
    exc: BaseException,
) -> None:
    errors.append(
        ScanErrorRecord(
            relative_path=relative_path,
            operation=operation,
            error_type=type(exc).__name__,
            message=str(exc),
        )
    )


# 파일 속성에 따라 사람이 검토할 정리 규칙을 고른다.
def _cleanup_rules(record: FileRecord, old_days: int) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    filename_cf = record.filename.casefold()
    stem_cf = Path(record.filename).stem.casefold()

    if record.filename.startswith("~$"):
        candidates.append(
            CleanupCandidate(
                record.file_id,
                record.relative_path,
                "office_lock_file",
                "Office 임시 잠금 파일명(~$)입니다. 열려 있는 문서인지 확인하십시오.",
            )
        )

    if record.extension in _TEMP_OR_BACKUP_EXTENSIONS:
        candidates.append(
            CleanupCandidate(
                record.file_id,
                record.relative_path,
                "temporary_or_backup_extension",
                f"임시/백업 가능성이 있는 확장자({record.extension})입니다.",
            )
        )

    if record.age_days >= old_days:
        candidates.append(
            CleanupCandidate(
                record.file_id,
                record.relative_path,
                "old_file",
                f"마지막 수정 후 {record.age_days:,}일이 지났습니다.",
            )
        )

    if record.category == "executable_installer":
        candidates.append(
            CleanupCandidate(
                record.file_id,
                record.relative_path,
                "installer_or_executable",
                "설치파일 또는 실행파일입니다. 업무 문서와 분리 보관할 후보입니다.",
            )
        )

    if _COPY_NAME_RE.search(stem_cf) or filename_cf.startswith("copy of "):
        candidates.append(
            CleanupCandidate(
                record.file_id,
                record.relative_path,
                "possible_copy_name",
                "파일명에 복사본으로 추정되는 표현이 있습니다.",
            )
        )

    return candidates


# 파일을 읽기 전용으로 읽어 내용 지문을 계산하고 변경 여부를 확인한다.
def _hash_file(record: FileRecord, errors: list[ScanErrorRecord]) -> str:
    """파일을 읽기 전용으로 읽어 내용 지문을 계산하고, 도중에 바뀌면 결과를 버린다."""
    try:
        before = record._absolute_path.stat()
        digest = hashlib.sha256()
        with record._absolute_path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = record._absolute_path.stat()
    except OSError as exc:
        record.hash_status = "error"
        _record_error(errors, record.relative_path, "sha256", exc)
        return ""

    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or after.st_size != record.size_bytes
        or after.st_mtime_ns != record._mtime_ns
    ):
        record.hash_status = "changed_during_hash"
        _record_error(
            errors,
            record.relative_path,
            "sha256",
            RuntimeError("file changed while hashing; hash discarded"),
        )
        return ""

    record.hash_status = "hashed"
    return digest.hexdigest()


# 같은 크기의 후보를 비교해 실제 중복 파일 묶음을 찾는다.
def _detect_duplicates(
    files: list[FileRecord],
    config: ScannerConfig,
    errors: list[ScanErrorRecord],
) -> list[DuplicateGroup]:
    if config.hash_mode == "none":
        return []

    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in files:
        if record.size_bytes > 0:
            by_size[record.size_bytes].append(record)
        else:
            record.hash_status = "zero_length_not_hashed"

    for size, records in by_size.items():
        if len(records) < 2:
            records[0].hash_status = "not_size_candidate"
            continue
        for record in records:
            if record.is_reparse_point:
                # OneDrive 등 클라우드 자리표시자 파일은 읽으면 내려받기가
                # 발생할 수 있으므로 자동 해시 대상에서 제외한다.
                record.hash_status = "skipped_reparse_point"
                continue
            if config.max_hash_size_bytes and size > config.max_hash_size_bytes:
                record.hash_status = "skipped_size_limit"
                continue
            record.sha256 = _hash_file(record, errors)

    duplicate_candidates: list[tuple[int, str, list[FileRecord]]] = []
    hashed_groups: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    for record in files:
        if record.sha256:
            hashed_groups[(record.size_bytes, record.sha256)].append(record)

    for (size, digest), records in hashed_groups.items():
        if len(records) >= 2:
            records.sort(key=lambda item: item.relative_path.casefold())
            duplicate_candidates.append((size, digest, records))

    duplicate_candidates.sort(key=lambda item: (-item[0], item[1]))
    groups: list[DuplicateGroup] = []
    for index, (size, digest, records) in enumerate(duplicate_candidates, start=1):
        group_id = f"DUP-{index:06d}"
        for record in records:
            record.duplicate_group_id = group_id
        groups.append(
            DuplicateGroup(
                group_id=group_id,
                sha256=digest,
                size_bytes=size,
                member_count=len(records),
                total_bytes=size * len(records),
                reclaimable_bytes=size * (len(records) - 1),
                file_ids=tuple(record.file_id for record in records),
                relative_paths=tuple(record.relative_path for record in records),
            )
        )
    return groups


# 누적값을 정렬된 폴더 결과 목록으로 변환한다.
def _folder_records(
    accumulators: dict[str, _FolderAccumulator],
) -> list[FolderRecord]:
    ordered = sorted(
        accumulators.values(),
        key=lambda item: (-item.depth, item.relative_path.casefold()),
    )

    for accumulator in ordered:
        accumulator.recursive_file_count += accumulator.direct_file_count
        accumulator.recursive_size_bytes += accumulator.direct_size_bytes
        if accumulator.relative_path == ".":
            continue
        parent_path = _parent_relative(accumulator.relative_path)
        parent = accumulators.get(parent_path)
        if parent is None:
            continue
        parent.recursive_file_count += accumulator.recursive_file_count
        parent.recursive_size_bytes += accumulator.recursive_size_bytes
        parent.recursive_folder_count += accumulator.recursive_folder_count + 1
        parent.latest_modified_epoch = max(
            parent.latest_modified_epoch, accumulator.latest_modified_epoch
        )

    records = [
        FolderRecord(
            relative_path=item.relative_path,
            depth=item.depth,
            direct_file_count=item.direct_file_count,
            recursive_file_count=item.recursive_file_count,
            direct_folder_count=item.direct_folder_count,
            recursive_folder_count=item.recursive_folder_count,
            direct_size_bytes=item.direct_size_bytes,
            recursive_size_bytes=item.recursive_size_bytes,
            latest_modified_at=_format_local_timestamp(item.latest_modified_epoch)
            if item.latest_modified_epoch
            else "",
            is_physically_empty=item.is_physically_empty,
            is_hidden=item.is_hidden,
            is_system=item.is_system,
        )
        for item in accumulators.values()
    ]
    records.sort(key=lambda item: (item.depth, item.relative_path.casefold()))
    return records


# 대상 폴더를 바꾸지 않고 파일과 폴더의 현황을 수집한다.
def scan_directory(config: ScannerConfig) -> ScanResult:
    """대상 폴더를 조회해 메모리상의 스캔 결과를 돌려준다.

    원본과 보고서 폴더를 포함해 어떤 파일도 만들지 않는다.
    """

    root, _ = ensure_output_outside_root(config.root, config.output_base)
    started_epoch = time.time()
    started_at = _format_local_timestamp(started_epoch)

    files: list[FileRecord] = []
    excluded: list[ExcludedRecord] = []
    errors: list[ScanErrorRecord] = []
    cleanup_candidates: list[CleanupCandidate] = []
    accumulators: dict[str, _FolderAccumulator] = {}

    try:
        root_stat = root.stat()
        root_hidden, root_system, _, _ = _item_flags(root.name, root_stat)
        root_latest = root_stat.st_mtime
    except OSError as exc:
        _record_error(errors, ".", "stat_root", exc)
        root_hidden = False
        root_system = False
        root_latest = 0.0

    accumulators["."] = _FolderAccumulator(
        relative_path=".",
        depth=0,
        latest_modified_epoch=root_latest,
        is_hidden=root_hidden,
        is_system=root_system,
    )

    stack: list[Path] = [root]
    scanned_file_count = 0
    now_epoch = time.time()

    while stack:
        directory = stack.pop()
        relative_directory = _relative_path(directory, root)
        accumulator = accumulators[relative_directory]

        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            _record_error(errors, relative_directory, "scandir", exc)
            continue

        accumulator.is_physically_empty = len(entries) == 0

        for entry in entries:
            entry_path = Path(entry.path)
            relative = _relative_path(entry_path, root)

            try:
                stat_result = entry.stat(follow_symlinks=False)
            except OSError as exc:
                _record_error(errors, relative, "stat", exc)
                continue

            is_hidden, is_system, is_readonly, is_reparse = _item_flags(
                entry.name, stat_result
            )

            try:
                is_link = entry.is_symlink()
            except OSError:
                is_link = is_reparse

            is_directory = stat_module.S_ISDIR(stat_result.st_mode)
            if is_link or (is_reparse and is_directory):
                excluded.append(
                    ExcludedRecord(
                        relative_path=relative,
                        item_type="link_or_reparse_point",
                        reason=(
                            "link_not_followed"
                            if is_link
                            else "directory_reparse_point_not_followed"
                        ),
                    )
                )
                continue

            exclusion = _exclude_reason(
                relative,
                entry.name,
                is_directory=is_directory,
                config=config,
            )
            if exclusion is not None:
                reason, pattern = exclusion
                excluded.append(
                    ExcludedRecord(
                        relative_path=relative,
                        item_type="directory" if is_directory else "file",
                        reason=reason,
                        matched_pattern=pattern,
                    )
                )
                continue

            if is_directory:
                depth = len(PurePosixPath(relative).parts)
                accumulators[relative] = _FolderAccumulator(
                    relative_path=relative,
                    depth=depth,
                    latest_modified_epoch=stat_result.st_mtime,
                    is_hidden=is_hidden,
                    is_system=is_system,
                )
                accumulator.direct_folder_count += 1
                stack.append(entry_path)
                continue

            if not stat_module.S_ISREG(stat_result.st_mode):
                excluded.append(
                    ExcludedRecord(
                        relative_path=relative,
                        item_type="special_file",
                        reason="non_regular_file_not_scanned",
                    )
                )
                continue

            extension = entry_path.suffix.casefold()
            created_at, created_source = _created_timestamp(stat_result)
            age_days = max(0, int((now_epoch - stat_result.st_mtime) // 86400))
            parent_path = _parent_relative(relative)
            record = FileRecord(
                file_id=stable_file_id(relative),
                relative_path=relative,
                parent_path=parent_path,
                filename=entry.name,
                extension=extension,
                category=classify_extension(extension),
                size_bytes=stat_result.st_size,
                size_human=human_size(stat_result.st_size),
                created_at=created_at,
                created_time_source=created_source,
                modified_at=_format_local_timestamp(stat_result.st_mtime),
                age_days=age_days,
                is_hidden=is_hidden,
                is_system=is_system,
                is_readonly=is_readonly,
                is_reparse_point=is_reparse,
                _absolute_path=entry_path,
                _mtime_ns=stat_result.st_mtime_ns,
            )
            files.append(record)
            cleanup_candidates.extend(_cleanup_rules(record, config.old_days))

            accumulator.direct_file_count += 1
            accumulator.direct_size_bytes += stat_result.st_size
            accumulator.latest_modified_epoch = max(
                accumulator.latest_modified_epoch, stat_result.st_mtime
            )

            scanned_file_count += 1
            if config.progress_every and scanned_file_count % config.progress_every == 0:
                print(f"[진행] 파일 {scanned_file_count:,}개 조회 완료", flush=True)

    files.sort(key=lambda item: item.relative_path.casefold())
    excluded.sort(key=lambda item: item.relative_path.casefold())
    cleanup_candidates.sort(
        key=lambda item: (item.relative_path.casefold(), item.rule)
    )

    duplicate_groups = _detect_duplicates(files, config, errors)
    errors.sort(key=lambda item: (item.relative_path.casefold(), item.operation))
    folders = _folder_records(accumulators)

    finished_epoch = time.time()
    return ScanResult(
        files=files,
        folders=folders,
        excluded=excluded,
        errors=errors,
        cleanup_candidates=cleanup_candidates,
        duplicate_groups=duplicate_groups,
        started_at=started_at,
        finished_at=_format_local_timestamp(finished_epoch),
        elapsed_seconds=finished_epoch - started_epoch,
        status="completed",
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

_FILE_MANIFEST_FIELDS = (
    "file_id",
    "relative_path",
    "parent_path",
    "filename",
    "extension",
    "category",
    "size_bytes",
    "size_human",
    "created_at",
    "created_time_source",
    "modified_at",
    "age_days",
    "is_hidden",
    "is_system",
    "is_readonly",
    "is_reparse_point",
    "hash_status",
    "sha256",
    "duplicate_group_id",
)


# 파일 결과를 보고서에 쓸 사전 형태로 변환한다.
def _file_record_dict(record: FileRecord) -> dict[str, Any]:
    return {
        "file_id": record.file_id,
        "relative_path": record.relative_path,
        "parent_path": record.parent_path,
        "filename": record.filename,
        "extension": record.extension,
        "category": record.category,
        "size_bytes": record.size_bytes,
        "size_human": record.size_human,
        "created_at": record.created_at,
        "created_time_source": record.created_time_source,
        "modified_at": record.modified_at,
        "age_days": record.age_days,
        "is_hidden": record.is_hidden,
        "is_system": record.is_system,
        "is_readonly": record.is_readonly,
        "is_reparse_point": record.is_reparse_point,
        "hash_status": record.hash_status,
        "sha256": record.sha256,
        "duplicate_group_id": record.duplicate_group_id,
    }


# 폴더 결과를 보고서에 쓸 사전 형태로 변환한다.
def _folder_record_dict(record: FolderRecord) -> dict[str, Any]:
    return {
        "relative_path": record.relative_path,
        "depth": record.depth,
        "direct_file_count": record.direct_file_count,
        "recursive_file_count": record.recursive_file_count,
        "direct_folder_count": record.direct_folder_count,
        "recursive_folder_count": record.recursive_folder_count,
        "direct_size_bytes": record.direct_size_bytes,
        "direct_size_human": human_size(record.direct_size_bytes),
        "recursive_size_bytes": record.recursive_size_bytes,
        "recursive_size_human": human_size(record.recursive_size_bytes),
        "latest_modified_at": record.latest_modified_at,
        "is_physically_empty": record.is_physically_empty,
        "is_hidden": record.is_hidden,
        "is_system": record.is_system,
    }


# 문자열에 포함된 로컬 절대경로를 안전한 표시로 치환한다.
def _redact_local_paths(value: str, config: ScannerConfig) -> str:
    """오류 메시지 등에 섞인 절대경로를 보고서용 표시로 바꾼다."""
    result = value
    replacements: list[tuple[str, str]] = []
    for path, token in (
        (Path(config.root).expanduser().resolve(strict=False), "<SCAN_ROOT>"),
        (Path(config.output_base).expanduser().resolve(strict=False), "<REPORT_BASE>"),
    ):
        path_text = str(path)
        replacements.extend(
            [
                (path_text, token),
                (path_text.replace("\\", "/"), token),
                (path_text.replace("/", "\\"), token),
            ]
        )
    for raw, token in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if raw:
            result = result.replace(raw, token)
    return result


# 쉼표 구분값의 셀을 안전한 문자열로 정리한다.
def _csv_cell(value: Any, config: ScannerConfig) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return excel_safe(_redact_local_paths(value, config))
    return value


# 행 목록을 한글 호환 인코딩의 쉼표 구분 파일로 저장한다.
def _write_csv(
    path: Path,
    fieldnames: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
    config: ScannerConfig,
) -> None:
    fields = list(fieldnames)
    with path.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_cell(row.get(field, ""), config) for field in fields})


# 문자열을 지정한 경로에 새 텍스트 파일로 저장한다.
def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8-sig", newline="\n") as destination:
        destination.write(text)


# 기존 결과와 겹치지 않는 보고서 폴더를 만든다.
def _create_unique_report_dir(output_base: Path) -> Path:
    output_base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("scan_%Y%m%d_%H%M%S")
    for index in range(10000):
        suffix = "" if index == 0 else f"_{index:02d}"
        candidate = output_base / f"{stamp}{suffix}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("could not create a unique report directory")


# 크기가 같은 파일 후보를 보고서 행으로 구성한다.
def _same_size_rows(files: list[FileRecord]) -> list[dict[str, Any]]:
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in files:
        if record.size_bytes > 0:
            by_size[record.size_bytes].append(record)

    groups = [
        (size, sorted(records, key=lambda item: item.relative_path.casefold()))
        for size, records in by_size.items()
        if len(records) >= 2
    ]
    groups.sort(key=lambda item: (-item[0], item[1][0].relative_path.casefold()))

    rows: list[dict[str, Any]] = []
    for index, (size, records) in enumerate(groups, start=1):
        candidate_group_id = f"SIZE-{index:06d}"
        all_members_hashed = all(
            record.hash_status == "hashed" and bool(record.sha256)
            for record in records
        )
        for record in records:
            if record.duplicate_group_id:
                confirmation = "confirmed_duplicate"
            elif record.hash_status == "not_requested":
                confirmation = "unconfirmed_hash_disabled"
            elif record.hash_status == "skipped_size_limit":
                confirmation = "unconfirmed_size_limit"
            elif record.hash_status == "hashed":
                confirmation = (
                    "same_size_but_different_content"
                    if all_members_hashed
                    else "partially_hashed_unconfirmed"
                )
            else:
                confirmation = record.hash_status
            rows.append(
                {
                    "candidate_group_id": candidate_group_id,
                    "member_count": len(records),
                    "size_bytes": size,
                    "size_human": human_size(size),
                    "file_id": record.file_id,
                    "relative_path": record.relative_path,
                    "hash_status": record.hash_status,
                    "sha256": record.sha256,
                    "duplicate_group_id": record.duplicate_group_id,
                    "confirmation_status": confirmation,
                }
            )
    return rows


# 확인된 중복 파일을 보고서 행으로 구성한다.
def _confirmed_duplicate_rows(groups: list[DuplicateGroup]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        for file_id, relative_path in zip(group.file_ids, group.relative_paths, strict=True):
            rows.append(
                {
                    "duplicate_group_id": group.group_id,
                    "sha256": group.sha256,
                    "member_count": group.member_count,
                    "size_bytes_each": group.size_bytes,
                    "size_human_each": human_size(group.size_bytes),
                    "total_bytes": group.total_bytes,
                    "reclaimable_bytes_theoretical": group.reclaimable_bytes,
                    "file_id": file_id,
                    "relative_path": relative_path,
                }
            )
    return rows


# 스캔 결과의 핵심 통계를 읽기 쉬운 요약문으로 만든다.
def _summary_text(result: ScanResult, config: ScannerConfig) -> str:
    total_bytes = sum(record.size_bytes for record in result.files)
    category_counts = Counter(record.category for record in result.files)
    extension_counts = Counter(record.extension or "[확장자 없음]" for record in result.files)
    size_counts = Counter(
        record.size_bytes for record in result.files if record.size_bytes > 0
    )
    same_size_group_count = sum(1 for count in size_counts.values() if count >= 2)
    reclaimable = sum(group.reclaimable_bytes for group in result.duplicate_groups)

    category_lines = "\n".join(
        f"  - {category}: {count:,}개"
        for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
    ) or "  - 없음"
    extension_lines = "\n".join(
        f"  - {extension}: {count:,}개"
        for extension, count in extension_counts.most_common(20)
    ) or "  - 없음"

    return f"""업무 파일/폴더 조회 전용 스캔 요약
====================================

[안전 범위]
- 스캔 대상은 <SCAN_ROOT>로 표기했습니다.
- 스캔 대상의 파일/폴더를 이동, 삭제, 이름 변경 또는 수정하지 않았습니다.
- 보고서는 스캔 대상 외부에만 생성되었습니다.
- 파일 내용 해시 모드: {config.hash_mode}
- hash_mode=none이면 파일 내용을 열지 않고 메타데이터만 조회합니다.
- hash_mode=duplicates이면 동일 크기 후보 파일만 읽기 전용으로 SHA-256을 계산합니다.

[실행 결과]
- 상태: {result.status}
- 시작: {result.started_at}
- 종료: {result.finished_at}
- 소요시간: {result.elapsed_seconds:.3f}초
- 파일: {len(result.files):,}개
- 폴더: {len(result.folders):,}개(스캔 루트 포함)
- 파일 총 논리 용량: {total_bytes:,} bytes ({human_size(total_bytes)})
- 실제 빈 폴더: {sum(
    1
    for folder in result.folders
    if folder.relative_path != "." and folder.is_physically_empty
):,}개
- 제외 항목: {len(result.excluded):,}개
- 조회 오류: {len(result.errors):,}건
- 정리 검토 후보: {len(result.cleanup_candidates):,}건(파일 수가 아니라 규칙 적중 건수)
- 동일 크기 후보 그룹: {same_size_group_count:,}개
- SHA-256 확인 중복 그룹: {len(result.duplicate_groups):,}개
- 이론상 중복 절감 가능 용량: {reclaimable:,} bytes ({human_size(reclaimable)})

[유형별 파일 수]
{category_lines}

[상위 확장자 20개]
{extension_lines}

[AI에 전달할 때]
1. 먼저 inventory_summary.txt와 folder_summary.csv를 검토합니다.
2. 상세 분류가 필요할 때 file_manifest.csv 또는 file_manifest.jsonl을 사용합니다.
3. 파일명과 폴더명 자체가 업무정보일 수 있으므로 외부 반출 규정을 확인합니다.
4. AI가 반환하는 결과는 file_id를 기준으로 작성하게 하십시오.
5. 삭제는 자동화하지 말고 MOVE/KEEP/REVIEW/ARCHIVE/QUARANTINE만 사용하십시오.
"""


# 트리 보고서에 표시할 항목 이름을 정한다.
def _display_name(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


# 폴더와 파일을 계층형 텍스트 보고서로 만든다.
def _tree_text(result: ScanResult) -> str:
    folders_by_parent: dict[str, list[FolderRecord]] = defaultdict(list)
    files_by_parent: dict[str, list[FileRecord]] = defaultdict(list)

    for folder in result.folders:
        if folder.relative_path == ".":
            continue
        folders_by_parent[_parent_relative(folder.relative_path)].append(folder)
    for record in result.files:
        files_by_parent[record.parent_path].append(record)

    for records in folders_by_parent.values():
        records.sort(key=lambda item: PurePosixPath(item.relative_path).name.casefold())
    for records in files_by_parent.values():
        records.sort(key=lambda item: item.filename.casefold())

    lines = [
        "<SCAN_ROOT>",
        "※ 상대경로만 표시합니다. 원본 파일에는 어떠한 변경도 하지 않았습니다.",
    ]

    # 현재 폴더부터 하위 항목까지 들여쓰기를 적용해 출력한다.
    def render(parent: str, prefix: str) -> None:
        children: list[tuple[str, FolderRecord | FileRecord]] = []
        children.extend(("folder", item) for item in folders_by_parent.get(parent, []))
        children.extend(("file", item) for item in files_by_parent.get(parent, []))

        for index, (kind, item) in enumerate(children):
            is_last = index == len(children) - 1
            connector = "└─" if is_last else "├─"
            next_prefix = prefix + ("   " if is_last else "│  ")

            if kind == "folder":
                folder = item
                assert isinstance(folder, FolderRecord)
                name = _display_name(PurePosixPath(folder.relative_path).name)
                empty_note = " | 실제 빈 폴더" if folder.is_physically_empty else ""
                lines.append(
                    f"{prefix}{connector} [폴더] {name}"
                    f" | 파일 {folder.recursive_file_count:,}개"
                    f" | {human_size(folder.recursive_size_bytes)}{empty_note}"
                )
                render(folder.relative_path, next_prefix)
            else:
                record = item
                assert isinstance(record, FileRecord)
                lines.append(
                    f"{prefix}{connector} [파일] {_display_name(record.filename)}"
                    f" | ID={record.file_id}"
                    f" | 유형={record.category}"
                    f" | 크기={record.size_human}"
                    f" | 수정={record.modified_at}"
                )

    render(".", "")
    if len(lines) == 2:
        lines.append("[스캔된 항목 없음]")

    lines.extend(
        [
            "",
            f"제외 항목: {len(result.excluded):,}개 — excluded_paths.csv 참조",
            f"조회 오류: {len(result.errors):,}건 — scan_errors.csv 참조",
        ]
    )
    return "\n".join(lines) + "\n"

# 생성된 결과 폴더의 확인 순서와 주의사항을 작성한다.
def _readme_text(config: ScannerConfig) -> str:
    hash_note = (
        "동일 크기 후보 파일을 읽기 전용으로 SHA-256 확인했습니다."
        if config.hash_mode == "duplicates"
        else "파일 내용을 열지 않았습니다. 동일 크기 후보는 아직 실제 중복으로 확인되지 않았습니다."
    )
    return f"""먼저 읽어 주십시오
==================

이 폴더는 조회 전용 스캐너가 만든 보고서입니다.
스캔 대상 원본에는 이동·삭제·이름 변경·내용 수정 작업을 하지 않았습니다.
{hash_note}

권장 확인 순서
1. inventory_summary.txt
2. folder_tree.txt
3. folder_summary.csv
4. file_manifest.csv
5. cleanup_candidates.csv
6. same_size_candidates.csv
7. confirmed_duplicates.csv(해시 확인을 실행한 경우)
8. scan_errors.csv

AI 분석용
- AI_ANALYSIS_PROMPT.txt의 지시문과 필요한 CSV를 함께 입력하십시오.
- 파일명/폴더명은 업무정보가 될 수 있습니다. 외부 반출 전 반드시 내부 규정을 확인하십시오.
- CSV는 Excel 수식 주입을 막기 위해 위험한 첫 문자가 있는 문자열 앞에 작은따옴표(')를 붙였습니다.
- 원래 문자열을 보존한 기계 판독용 자료는 file_manifest.jsonl입니다.

주의
- cleanup_candidates.csv는 삭제 목록이 아니라 사람 검토 후보입니다.
- same_size_candidates.csv는 크기만 같은 파일도 포함합니다.
- confirmed_duplicates.csv도 자동 삭제 근거로 사용하지 마십시오. 보존본과 참조 관계를 사람이 확인해야 합니다.
"""


# 스캔 결과를 인공지능에 분석 요청할 때 쓸 지시문을 만든다.
def _ai_prompt_text() -> str:
    return """당신은 업무 파일 정리 설계자다.

첨부한 파일 목록은 실제 파일 내용이 아니라 파일명, 상대경로, 확장자, 용량, 수정일 등의 메타데이터다. 다음 원칙을 지켜 분석하라.

1. 먼저 현재 업무의 성격을 추정해 2~3개의 목표 폴더 구조 대안을 제시하고, 가장 적합한 안을 추천하라.
2. 확신할 수 없는 파일은 억지로 분류하지 말고 REVIEW로 지정하라.
3. 파일 삭제를 제안하거나 실행 대상으로 지정하지 마라.
4. 허용 action은 KEEP, MOVE, REVIEW, ARCHIVE, QUARANTINE뿐이다.
5. 파일 식별은 파일명 재작성 대신 file_id를 사용하라.
6. 입력에 없는 file_id를 만들거나, 파일을 누락하지 마라.
7. 동일 이름 또는 유사 이름만으로 중복이라고 단정하지 마라. confirmed_duplicates.csv에서 동일 SHA-256으로 확인된 경우도 보존 필요성과 참조 관계를 별도로 검토하라.
8. 업무상 민감성, 보존기간, 공용 참조 가능성을 알 수 없으면 REVIEW로 남겨라.

먼저 폴더 구조 제안과 분류 규칙을 설명하라. 내가 구조를 승인한 뒤에만 파일별 이동안을 다음 CSV 열 순서로 출력하라.

file_id,action,target_folder,new_filename,confidence,reason

세부 규칙
- confidence는 0.00~1.00 숫자다.
- new_filename을 바꿀 근거가 없으면 빈 값으로 둔다.
- target_folder는 승인된 목표 구조의 상대경로만 사용한다.
- REVIEW와 KEEP은 target_folder를 비워도 된다.
- 결과 CSV 뒤에 누락 file_id 수, 중복 file_id 수, action별 건수를 검증표로 제시한다.
- 설명과 결과는 한국어로 작성한다.
"""


# 스캔 결과를 대상 밖의 새 보고서 폴더에 저장한다.
def write_reports(result: ScanResult, config: ScannerConfig) -> Path:
    """스캔 결과를 대상 밖의 새 보고서 폴더에 저장한다."""

    _, output_base = ensure_output_outside_root(config.root, config.output_base)
    report_dir = _create_unique_report_dir(output_base)

    manifest_rows = [_file_record_dict(record) for record in result.files]
    folder_rows = [_folder_record_dict(record) for record in result.folders]
    empty_rows = [
        _folder_record_dict(record)
        for record in result.folders
        if record.relative_path != "." and record.is_physically_empty
    ]
    large_rows = [
        _file_record_dict(record)
        for record in sorted(
            result.files,
            key=lambda item: (-item.size_bytes, item.relative_path.casefold()),
        )[: config.large_count]
    ]
    old_rows = [
        _file_record_dict(record)
        for record in sorted(
            (item for item in result.files if item.age_days >= config.old_days),
            key=lambda item: (-item.age_days, item.relative_path.casefold()),
        )
    ]
    cleanup_rows = [
        {
            "file_id": item.file_id,
            "relative_path": item.relative_path,
            "rule": item.rule,
            "reason": item.reason,
        }
        for item in result.cleanup_candidates
    ]
    same_size_rows = _same_size_rows(result.files)
    duplicate_rows = _confirmed_duplicate_rows(result.duplicate_groups)
    excluded_rows = [
        {
            "relative_path": item.relative_path,
            "item_type": item.item_type,
            "reason": item.reason,
            "matched_pattern": item.matched_pattern,
        }
        for item in result.excluded
    ]
    error_rows = [
        {
            "relative_path": item.relative_path,
            "operation": item.operation,
            "error_type": item.error_type,
            "message": item.message,
        }
        for item in result.errors
    ]

    _write_text(report_dir / "README_FIRST.txt", _readme_text(config))
    _write_text(report_dir / "inventory_summary.txt", _summary_text(result, config))
    _write_text(report_dir / "folder_tree.txt", _tree_text(result))
    _write_csv(report_dir / "file_manifest.csv", _FILE_MANIFEST_FIELDS, manifest_rows, config)

    with (report_dir / "file_manifest.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as destination:
        for row in manifest_rows:
            safe_row = {
                key: _redact_local_paths(value, config) if isinstance(value, str) else value
                for key, value in row.items()
            }
            destination.write(json.dumps(safe_row, ensure_ascii=False, sort_keys=False) + "\n")

    folder_fields = (
        "relative_path",
        "depth",
        "direct_file_count",
        "recursive_file_count",
        "direct_folder_count",
        "recursive_folder_count",
        "direct_size_bytes",
        "direct_size_human",
        "recursive_size_bytes",
        "recursive_size_human",
        "latest_modified_at",
        "is_physically_empty",
        "is_hidden",
        "is_system",
    )
    _write_csv(report_dir / "folder_summary.csv", folder_fields, folder_rows, config)
    _write_csv(report_dir / "empty_folders.csv", folder_fields, empty_rows, config)
    _write_csv(report_dir / "large_files.csv", _FILE_MANIFEST_FIELDS, large_rows, config)
    _write_csv(report_dir / "old_files.csv", _FILE_MANIFEST_FIELDS, old_rows, config)
    _write_csv(
        report_dir / "cleanup_candidates.csv",
        ("file_id", "relative_path", "rule", "reason"),
        cleanup_rows,
        config,
    )
    _write_csv(
        report_dir / "same_size_candidates.csv",
        (
            "candidate_group_id",
            "member_count",
            "size_bytes",
            "size_human",
            "file_id",
            "relative_path",
            "hash_status",
            "sha256",
            "duplicate_group_id",
            "confirmation_status",
        ),
        same_size_rows,
        config,
    )
    _write_csv(
        report_dir / "confirmed_duplicates.csv",
        (
            "duplicate_group_id",
            "sha256",
            "member_count",
            "size_bytes_each",
            "size_human_each",
            "total_bytes",
            "reclaimable_bytes_theoretical",
            "file_id",
            "relative_path",
        ),
        duplicate_rows,
        config,
    )
    _write_csv(
        report_dir / "excluded_paths.csv",
        ("relative_path", "item_type", "reason", "matched_pattern"),
        excluded_rows,
        config,
    )
    _write_csv(
        report_dir / "scan_errors.csv",
        ("relative_path", "operation", "error_type", "message"),
        error_rows,
        config,
    )

    metadata = {
        "scanner": "readonly_file_scanner",
        "schema_version": 1,
        "scan_root": "<SCAN_ROOT>",
        "status": result.status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "elapsed_seconds": round(result.elapsed_seconds, 6),
        "settings": {
            "hash_mode": config.hash_mode,
            "max_hash_size_bytes": config.max_hash_size_bytes,
            "old_days": config.old_days,
            "large_count": config.large_count,
            "use_default_excludes": config.use_default_excludes,
            "exclude_patterns": list(config.exclude_patterns),
        },
        "counts": {
            "files": len(result.files),
            "folders_including_root": len(result.folders),
            "excluded": len(result.excluded),
            "errors": len(result.errors),
            "cleanup_rule_hits": len(result.cleanup_candidates),
            "confirmed_duplicate_groups": len(result.duplicate_groups),
        },
    }
    with (report_dir / "scan_metadata.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as destination:
        destination.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    _write_text(report_dir / "AI_ANALYSIS_PROMPT.txt", _ai_prompt_text())
    return report_dir


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

SCANNER_VERSION = "1.0.0"


# 명령행에서 사용할 옵션과 도움말을 정의한다.
def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "업무 파일/폴더를 변경하지 않고 메타데이터 보고서를 생성합니다. "
            "보고서 경로는 반드시 스캔 대상 외부여야 합니다."
        )
    )
    parser.add_argument("root", help="조회할 최상위 폴더")
    parser.add_argument(
        "--output",
        required=True,
        help="보고서 상위 폴더. 스캔 대상 폴더 바깥을 지정해야 합니다.",
    )
    parser.add_argument(
        "--hash-duplicates",
        action="store_true",
        help=(
            "동일 크기 후보의 내용을 읽기 전용으로 SHA-256 확인합니다. "
            "기본값은 메타데이터 전용이며 파일 내용을 열지 않습니다."
        ),
    )
    parser.add_argument(
        "--max-hash-size-mb",
        type=int,
        default=4096,
        help="해시할 파일 한 개의 최대 크기(MiB). 0은 제한 없음. 기본값: 4096",
    )
    parser.add_argument(
        "--old-days",
        type=int,
        default=730,
        help="오래된 파일 후보 기준 일수. 기본값: 730",
    )
    parser.add_argument(
        "--large-count",
        type=int,
        default=100,
        help="large_files.csv에 기록할 상위 파일 수. 기본값: 100",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "제외할 이름 또는 상대경로 glob. 여러 번 지정할 수 있습니다. "
            "예: --exclude '완료/**' --exclude '*.iso'"
        ),
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help=".git, .venv, node_modules 등 기본 기술 폴더도 포함합니다.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=500,
        help="지정 개수마다 진행 상황을 표시합니다. 0이면 표시하지 않습니다. 기본값: 500",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCANNER_VERSION}")
    return parser


# 명령행 인수를 처리하고 스캔과 보고서 생성을 실행한다.
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        if args.max_hash_size_mb < 0:
            raise ValueError("--max-hash-size-mb는 0 이상이어야 합니다.")
        if args.old_days < 0:
            raise ValueError("--old-days는 0 이상이어야 합니다.")
        if args.large_count < 1:
            raise ValueError("--large-count는 1 이상이어야 합니다.")
        if args.progress_every < 0:
            raise ValueError("--progress-every는 0 이상이어야 합니다.")

        config = ScannerConfig(
            root=Path(args.root),
            output_base=Path(args.output),
            hash_mode="duplicates" if args.hash_duplicates else "none",
            max_hash_size_bytes=(
                0 if args.max_hash_size_mb == 0 else args.max_hash_size_mb * 1024**2
            ),
            old_days=args.old_days,
            large_count=args.large_count,
            exclude_patterns=tuple(args.exclude),
            use_default_excludes=not args.no_default_excludes,
            progress_every=args.progress_every,
        )

        root, output = ensure_output_outside_root(config.root, config.output_base)
        print("[안전] 원본 파일의 이동·삭제·이름 변경·내용 수정 기능이 없습니다.")
        print(f"[대상] {root}")
        print(f"[보고서 상위 폴더] {output}")
        if config.hash_mode == "duplicates":
            limit_text = (
                "제한 없음"
                if config.max_hash_size_bytes == 0
                else human_size(config.max_hash_size_bytes)
            )
            print(
                "[모드] 동일 크기 후보를 읽기 전용으로 SHA-256 확인 "
                f"(파일당 최대 {limit_text})"
            )
        else:
            print("[모드] 메타데이터 전용 — 파일 내용을 열지 않습니다.")

        result = scan_directory(config)
        report_dir = write_reports(result, config)
        print(
            f"[완료] 파일 {len(result.files):,}개, 폴더 {len(result.folders):,}개, "
            f"오류 {len(result.errors):,}건"
        )
        print(f"[결과] {report_dir}")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
