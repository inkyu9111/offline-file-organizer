#!/usr/bin/env python3
"""조회 전용 파일 스캐너의 명령행 동작과 데이터베이스 결과를 검증한다."""

from __future__ import annotations

import ast
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("readonly_file_scanner.py")
README_PATH = Path(__file__).with_name("README.md")
BATCH_PATH = Path(__file__).with_name("run_scanner.bat")


# 지정한 인수로 스캐너를 실행하고 표준 출력을 모아 돌려준다.
def run_scanner(*arguments: object, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    command = [sys.executable, str(SCRIPT_PATH), *(str(value) for value in arguments)]
    return subprocess.run(
        command,
        cwd=SCRIPT_PATH.parent,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


# 데이터베이스에서 첫 번째 행의 첫 번째 값을 읽는다.
def query_value(database_path: Path, sql: str, parameters: tuple[object, ...] = ()) -> object:
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise AssertionError(f"질의 결과가 없습니다: {sql}")
        return row[0]
    finally:
        connection.close()


# 파일의 내용 지문과 수정 시각을 함께 구한다.
def file_state(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    status = path.stat()
    return digest, status.st_size, status.st_mtime_ns


# 스캐너의 주요 요구사항을 실제 명령행 실행으로 확인한다.
class ScannerIntegrationTests(unittest.TestCase):
    # 각 시험에서 사용할 독립된 임시 폴더를 만든다.
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root_a = self.base / "업무자료가"
        self.root_b = self.base / "업무자료나"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.output = self.base / "결과" / "scan.sqlite3"

    # 시험이 끝나면 임시 폴더와 파일을 지운다.
    def tearDown(self) -> None:
        self.temporary.cleanup()

    # 도움말에 새 입출력 옵션이 모두 안내되는지 확인한다.
    def test_help_contains_sqlite_and_progress_options(self) -> None:
        completed = run_scanner("--help")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for text in (
            "--output",
            "--root",
            "--progress-interval",
            "--progress-every",
            "--commit-every",
            "--hash-duplicates",
        ):
            with self.subTest(text=text):
                self.assertIn(text, completed.stdout)

    # 위치 인수로 받은 여러 루트가 데이터베이스 하나에 합쳐지는지 확인한다.
    def test_multiple_positional_roots_write_one_database(self) -> None:
        (self.root_a / "가.txt").write_text("가", encoding="utf-8")
        (self.root_b / "나.txt").write_text("나", encoding="utf-8")

        completed = run_scanner(
            self.root_a,
            self.root_b,
            "--output",
            self.output,
            "--progress-interval",
            0,
            "--progress-every",
            1,
            "--commit-every",
            1,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(self.output.is_file())
        self.assertEqual(query_value(self.output, "SELECT COUNT(*) FROM scan_roots"), 2)
        self.assertEqual(query_value(self.output, "SELECT COUNT(*) FROM files"), 2)
        self.assertEqual(
            query_value(self.output, "SELECT COUNT(DISTINCT root_id) FROM files"), 2
        )

    # 반복 옵션으로 받은 여러 루트도 같은 방식으로 처리되는지 확인한다.
    def test_repeated_root_options_are_supported(self) -> None:
        (self.root_a / "가.txt").write_text("가", encoding="utf-8")
        (self.root_b / "나.txt").write_text("나", encoding="utf-8")

        completed = run_scanner(
            "--root",
            self.root_a,
            "--root",
            self.root_b,
            "--output",
            self.output,
            "--progress-interval",
            0,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(query_value(self.output, "SELECT COUNT(*) FROM scan_roots"), 2)

    # 폴더 하나만 지정하는 기존 사용법도 유지되는지 확인한다.
    def test_single_root_is_still_supported(self) -> None:
        (self.root_a / "문서.txt").write_text("내용", encoding="utf-8")

        completed = run_scanner(
            self.root_a,
            "--output",
            self.output,
            "--progress-interval",
            0,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(query_value(self.output, "SELECT COUNT(*) FROM scan_roots"), 1)
        self.assertEqual(query_value(self.output, "SELECT COUNT(*) FROM files"), 1)

    # 출력 파일이 어느 스캔 루트 안에 있어도 실행을 거부하는지 확인한다.
    def test_output_inside_any_root_is_rejected(self) -> None:
        unsafe_output = self.root_b / "scan.sqlite3"

        completed = run_scanner(
            self.root_a,
            self.root_b,
            "--output",
            unsafe_output,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(unsafe_output.exists())

    # 상위 폴더와 그 하위 폴더를 함께 지정하면 중복 스캔을 막는지 확인한다.
    def test_overlapping_roots_are_rejected(self) -> None:
        nested = self.root_a / "하위"
        nested.mkdir()

        completed = run_scanner(
            self.root_a,
            nested,
            "--output",
            self.output,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(self.output.exists())

    # 기존 데이터베이스를 덮어쓰지 않고 원래 내용을 보존하는지 확인한다.
    def test_existing_output_is_not_overwritten(self) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b"기존 결과")
        before = self.output.read_bytes()

        completed = run_scanner(self.root_a, "--output", self.output)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.output.read_bytes(), before)

    # 화면 진행 문구에 현재 처리 중인 폴더가 표시되는지 확인한다.
    def test_progress_shows_current_folder(self) -> None:
        nested = self.root_a / "사업" / "보고서"
        nested.mkdir(parents=True)
        (nested / "문서.txt").write_text("내용", encoding="utf-8")

        completed = run_scanner(
            self.root_a,
            "--output",
            self.output,
            "--progress-interval",
            0,
            "--progress-every",
            1,
            "--commit-every",
            1,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        combined = completed.stdout + completed.stderr
        self.assertIn("[진행]", combined)
        self.assertIn("현재 폴더", combined)
        self.assertIn("파일", combined)

    # 전체 실행 상태와 누적 건수가 데이터베이스에 남는지 확인한다.
    def test_scan_run_records_final_status_and_counts(self) -> None:
        (self.root_a / "문서.txt").write_text("내용", encoding="utf-8")
        completed = run_scanner(
            self.root_a,
            "--output",
            self.output,
            "--commit-every",
            1,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        connection = sqlite3.connect(self.output)
        try:
            row = connection.execute(
                "SELECT status, files_scanned, folders_discovered, "
                "folders_completed, last_heartbeat_at FROM scan_run WHERE run_id = 1"
            ).fetchone()
        finally:
            connection.close()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn(row[0], {"completed", "completed_with_errors"})
        self.assertEqual(row[1], 1)
        self.assertGreaterEqual(row[2], 1)
        self.assertGreaterEqual(row[3], 1)
        self.assertTrue(row[4])

    # 폴더 처리 시작과 완료 이력이 진단용 표에 기록되는지 확인한다.
    def test_scan_events_record_folder_activity(self) -> None:
        nested = self.root_a / "하위"
        nested.mkdir()
        completed = run_scanner(
            self.root_a,
            "--output",
            self.output,
            "--commit-every",
            1,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        event_count = query_value(self.output, "SELECT COUNT(*) FROM scan_events")
        self.assertGreaterEqual(event_count, 2)

    # 기본 모드에서는 파일 내용 지문이 계산되지 않는지 확인한다.
    def test_default_mode_does_not_hash_file_contents(self) -> None:
        (self.root_a / "문서.txt").write_text("내용", encoding="utf-8")
        completed = run_scanner(self.root_a, "--output", self.output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        hashed = query_value(
            self.output,
            "SELECT COUNT(*) FROM files WHERE COALESCE(sha256, '') <> ''",
        )
        self.assertEqual(hashed, 0)

    # 선택한 경우에만 실제 중복 파일이 내용 지문으로 확인되는지 검사한다.
    def test_duplicate_hashing_works_across_roots(self) -> None:
        payload = b"같은 파일 내용"
        (self.root_a / "복사본가.bin").write_bytes(payload)
        (self.root_b / "복사본나.bin").write_bytes(payload)

        completed = run_scanner(
            self.root_a,
            self.root_b,
            "--output",
            self.output,
            "--hash-duplicates",
            "--max-hash-size-mb",
            10,
            "--commit-every",
            1,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            query_value(
                self.output,
                "SELECT COUNT(*) FROM files WHERE COALESCE(sha256, '') <> ''",
            ),
            2,
        )
        self.assertEqual(
            query_value(self.output, "SELECT COUNT(*) FROM v_confirmed_duplicates"),
            2,
        )

    # 기본 제외 폴더가 내려가지 않고 제외 이력에 남는지 확인한다.
    def test_default_excluded_directory_is_recorded(self) -> None:
        hidden = self.root_a / ".git"
        hidden.mkdir()
        (hidden / "config").write_text("설정", encoding="utf-8")

        completed = run_scanner(self.root_a, "--output", self.output)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(query_value(self.output, "SELECT COUNT(*) FROM files"), 0)
        self.assertGreaterEqual(
            query_value(self.output, "SELECT COUNT(*) FROM excluded_paths"), 1
        )

    # 임시파일과 오래된 파일 규칙이 검토 후보 표에 기록되는지 확인한다.
    def test_cleanup_candidates_are_recorded(self) -> None:
        candidate = self.root_a / "오래된자료.bak"
        candidate.write_text("내용", encoding="utf-8")
        old_time = 946684800
        os.utime(candidate, (old_time, old_time))

        completed = run_scanner(
            self.root_a,
            "--output",
            self.output,
            "--old-days",
            30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertGreaterEqual(
            query_value(self.output, "SELECT COUNT(*) FROM cleanup_candidates"), 1
        )

    # 스캔 전후에 원본 파일의 내용과 수정 시각이 그대로인지 확인한다.
    def test_source_files_are_not_modified(self) -> None:
        original = self.root_a / "원본.txt"
        original.write_text("변경하면 안 되는 내용", encoding="utf-8")
        before = file_state(original)

        completed = run_scanner(
            self.root_a,
            "--output",
            self.output,
            "--hash-duplicates",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(file_state(original), before)

    # 생성된 데이터베이스가 손상 없이 열리는지 검사한다.
    def test_database_integrity_check_passes(self) -> None:
        (self.root_a / "문서.txt").write_text("내용", encoding="utf-8")
        completed = run_scanner(self.root_a, "--output", self.output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(query_value(self.output, "PRAGMA integrity_check"), "ok")

    # 분석에 필요한 핵심 표와 보기가 모두 만들어지는지 확인한다.
    def test_expected_tables_and_views_exist(self) -> None:
        completed = run_scanner(self.root_a, "--output", self.output)
        self.assertEqual(completed.returncode, 0, completed.stderr)

        connection = sqlite3.connect(self.output)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
        finally:
            connection.close()

        expected = {
            "scan_run",
            "scan_roots",
            "folders",
            "files",
            "cleanup_candidates",
            "excluded_paths",
            "scan_errors",
            "scan_events",
            "embedded_documents",
            "v_file_manifest",
            "v_folder_summary",
            "v_empty_folders",
            "v_old_files",
            "v_large_files",
            "v_same_size_candidates",
            "v_confirmed_duplicates",
        }
        self.assertTrue(expected.issubset(names), sorted(expected - names))

    # 데이터베이스 안에 사용 안내문이 함께 들어가는지 확인한다.
    def test_embedded_documents_are_present(self) -> None:
        completed = run_scanner(self.root_a, "--output", self.output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertGreaterEqual(
            query_value(self.output, "SELECT COUNT(*) FROM embedded_documents"), 2
        )


# 저장소 문서와 주석이 프로젝트 작성 규칙을 따르는지 확인한다.
class RepositoryStyleTests(unittest.TestCase):
    # 사용설명서에 다중 루트와 데이터베이스 사용법이 들어 있는지 확인한다.
    def test_readme_contains_current_usage(self) -> None:
        content = README_PATH.read_text(encoding="utf-8")
        required = (
            "SQLite 데이터베이스 파일 하나",
            "py -3 readonly_file_scanner.py",
            "--root",
            "--output",
            "--progress-interval",
            "--commit-every",
            "scan_run",
            "v_file_manifest",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, content)
        self.assertNotIn("한 줄씨", content)

    # 배치파일이 여러 루트와 데이터베이스 출력값을 넘기는지 확인한다.
    def test_batch_file_supports_multiple_roots(self) -> None:
        content = BATCH_PATH.read_text(encoding="utf-8", errors="replace")
        self.assertIn("--root", content)
        self.assertIn("--output", content)
        self.assertIn("readonly_file_scanner.py", content)

    # 데이터베이스 임시 작업이 메모리를 강제로 쓰지 않는지 확인한다.
    def test_database_temp_store_uses_disk(self) -> None:
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        normalized = " ".join(content.replace(";", " ").split()).casefold()
        self.assertIn("temp_store", normalized)
        self.assertNotIn("temp_store = memory", normalized)
        self.assertNotIn("temp_store=memory", normalized)

    # 모든 함수와 클래스 앞에 영문 없는 한글 역할 주석이 있는지 확인한다.
    def test_every_definition_has_korean_role_comment(self) -> None:
        for source_path in (SCRIPT_PATH, Path(__file__)):
            source = source_path.read_text(encoding="utf-8")
            lines = source.splitlines()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                start_line = min(
                    [node.lineno, *(item.lineno for item in node.decorator_list)]
                )
                previous = start_line - 2
                while previous >= 0 and not lines[previous].strip():
                    previous -= 1
                self.assertGreaterEqual(
                    previous,
                    0,
                    f"{source_path.name}:{start_line} {node.name} 역할 주석 누락",
                )
                comment = lines[previous].strip()
                self.assertTrue(
                    comment.startswith("# "),
                    f"{source_path.name}:{start_line} {node.name} 한 줄 주석 누락",
                )
                self.assertRegex(
                    comment,
                    r"[가-힣]",
                    f"{source_path.name}:{start_line} {node.name} 한글 없음",
                )
                self.assertNotRegex(
                    comment,
                    r"[A-Za-z]",
                    f"{source_path.name}:{start_line} {node.name} 영문 포함",
                )


# 명령행에서 직접 실행하면 전체 시험을 시작한다.
def main() -> None:
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
