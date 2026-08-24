import ast
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from readonly_file_scanner import (
    ProgressSnapshot,
    ScannerConfig,
    _ProgressPrinter,
    _SQLiteStore,
    _redact_paths,
    classify_extension,
    format_progress,
    human_size,
    main,
    scan_to_database,
    stable_file_id,
    stable_root_key,
    validate_scan_paths,
)


# 경로 검증과 식별자 생성 규칙을 점검한다.
class PathValidationTests(unittest.TestCase):
    # 여러 개의 서로 독립된 폴더를 입력으로 허용하는지 확인한다.
    def test_accepts_multiple_independent_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first = base / "업무1"
            second = base / "업무2"
            output = base / "결과" / "scan.sqlite3"
            first.mkdir()
            second.mkdir()

            roots, output_db = validate_scan_paths((first, second), output)

            self.assertEqual(roots, (first.resolve(), second.resolve()))
            self.assertEqual(output_db, output.resolve())

    # 출력 데이터베이스가 어느 스캔 폴더 안에 있어도 거부하는지 확인한다.
    def test_rejects_output_inside_any_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first = base / "업무1"
            second = base / "업무2"
            first.mkdir()
            second.mkdir()

            with self.assertRaisesRegex(ValueError, "스캔 대상 밖"):
                validate_scan_paths((first, second), second / "scan.sqlite3")

    # 같은 폴더를 중복 지정하면 이중 스캔을 막는지 확인한다.
    def test_rejects_duplicate_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "업무"
            root.mkdir()

            with self.assertRaisesRegex(ValueError, "중복"):
                validate_scan_paths((root, root), Path(temp_dir) / "scan.sqlite3")

    # 상위 폴더와 그 하위 폴더를 함께 지정하면 이중 스캔을 막는지 확인한다.
    def test_rejects_nested_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "업무"
            child = root / "하위"
            child.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "포함 관계"):
                validate_scan_paths((root, child), Path(temp_dir) / "scan.sqlite3")

    # 이미 존재하는 출력 파일을 덮어쓰지 않는지 확인한다.
    def test_rejects_existing_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "업무"
            output = Path(temp_dir) / "scan.sqlite3"
            root.mkdir()
            output.write_bytes("기존 자료".encode("utf-8"))

            with self.assertRaisesRegex(FileExistsError, "이미 존재"):
                validate_scan_paths((root,), output)

    # 같은 상대경로라도 서로 다른 스캔 루트에서는 식별자가 달라지는지 확인한다.
    def test_file_id_is_unique_across_roots(self):
        first_key = stable_root_key(Path("C:/업무1"))
        second_key = stable_root_key(Path("D:/업무2"))

        self.assertNotEqual(
            stable_file_id(first_key, "보고서/결과.xlsx"),
            stable_file_id(second_key, "보고서/결과.xlsx"),
        )

    # 대소문자와 경로 구분자가 달라도 같은 파일 식별자를 만드는지 확인한다.
    def test_file_id_is_stable_within_same_root(self):
        root_key = "R-ABC"

        self.assertEqual(
            stable_file_id(root_key, "Folder/Report.xlsx"),
            stable_file_id(root_key, "folder\\report.xlsx"),
        )


# 공통 보조 함수의 출력값을 점검한다.
class HelperTests(unittest.TestCase):
    # 상대경로의 구두점과 파일명이 일반 오류 문장에서 잘못 가려지지 않는지 확인한다.
    def test_path_redaction_does_not_replace_relative_input_text(self):
        current = Path.cwd()
        config = ScannerConfig(roots=(Path("."),), output_db=Path("scan.sqlite3"))
        message = "foo.py 실패. 버전 1.2이며 scan.sqlite3은 파일명이다."

        redacted = _redact_paths(
            message,
            (current,),
            current / "scan.sqlite3",
            config,
        )

        self.assertEqual(redacted, message)

    # 업무에서 자주 쓰는 확장자를 올바른 유형으로 분류하는지 확인한다.
    def test_classifies_common_business_extensions(self):
        self.assertEqual(classify_extension(".xlsx"), "spreadsheet")
        self.assertEqual(classify_extension(".hwpx"), "document")
        self.assertEqual(classify_extension(".pptx"), "presentation")
        self.assertEqual(classify_extension(".pdf"), "pdf")
        self.assertEqual(classify_extension(".zip"), "archive")
        self.assertEqual(classify_extension(""), "no_extension")
        self.assertEqual(classify_extension(".unexpected"), "other")

    # 파일 크기를 이진 단위로 읽기 쉽게 표시하는지 확인한다.
    def test_human_size_uses_binary_units(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024), "1.00 KiB")
        self.assertEqual(human_size(1024 * 1024), "1.00 MiB")

    # 진행 문구에 현재 폴더와 정확한 누적 건수가 들어가는지 확인한다.
    def test_progress_text_contains_current_folder_and_counts(self):
        snapshot = ProgressSnapshot(
            phase="metadata",
            roots_total=2,
            roots_completed=0,
            current_root_label="ROOT-001",
            current_folder="사업/2026/보고서",
            current_operation="폴더 항목 조회",
            folders_discovered=25,
            folders_completed=11,
            files_scanned=345,
            bytes_scanned=123456,
            excluded_count=4,
            error_count=2,
            elapsed_seconds=12.3,
        )

        text = format_progress(snapshot)

        self.assertIn("ROOT-001", text)
        self.assertIn("사업/2026/보고서", text)
        self.assertIn("완료 폴더 11/발견 25", text)
        self.assertIn("파일 345개", text)
        self.assertIn("오류 2건", text)

    # 터미널 폭보다 긴 현재 폴더도 잘라내지 않고 출력하는지 확인한다.
    def test_progress_printer_preserves_full_current_folder(self):
        # 실제 터미널처럼 한 줄 갱신 방식을 사용한다고 알린다.
        class TerminalBuffer(io.StringIO):
            # 출력기가 터미널용 경로를 사용하도록 참을 돌려준다.
            def isatty(self):
                return True

        current_folder = "/".join(["매우긴폴더이름"] * 40)
        snapshot = ProgressSnapshot(
            phase="metadata",
            roots_total=1,
            current_root_label="ROOT-001",
            current_folder=current_folder,
            current_operation="폴더 항목 조회",
        )
        output = TerminalBuffer()

        _ProgressPrinter(output).write(snapshot)

        self.assertIn(current_folder, output.getvalue())


# 여러 루트의 파일을 단일 데이터베이스에 기록하는 동작을 점검한다.
class SQLiteScanTests(unittest.TestCase):
    # 각 테스트에서 사용할 독립된 폴더 구조를 만든다.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root_a = self.base / "업무A"
        self.root_b = self.base / "업무B"
        self.output = self.base / "결과" / "scan.sqlite3"
        self.root_a.mkdir()
        self.root_b.mkdir()

        (self.root_a / "보고서").mkdir()
        (self.root_a / "보고서" / "결과.xlsx").write_bytes(b"abc")
        (self.root_a / "빈폴더").mkdir()
        (self.root_b / "자료").mkdir()
        (self.root_b / "자료" / "결과.xlsx").write_bytes(b"abc")
        (self.root_b / "자료" / "메모.txt").write_text("hello", encoding="utf-8")

    # 테스트가 끝나면 임시 폴더를 정리한다.
    def tearDown(self):
        self.temp.cleanup()

    # 데이터베이스에서 한 행의 값을 읽어오는 보조 기능을 제공한다.
    def _fetchone(self, sql, parameters=()):
        with closing(sqlite3.connect(self.output)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(sql, parameters).fetchone()

    # 여러 스캔 폴더가 루트별로 구분되어 한 파일에 저장되는지 확인한다.
    def test_scans_multiple_roots_into_one_database(self):
        summary = scan_to_database(
            ScannerConfig(
                roots=(self.root_a, self.root_b),
                output_db=self.output,
                progress_interval_seconds=0,
                progress_every_files=1,
                commit_every=1,
            ),
            progress_stream=io.StringIO(),
        )

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.root_count, 2)
        self.assertEqual(summary.file_count, 3)
        self.assertEqual(summary.folder_count, 5)

        with closing(sqlite3.connect(self.output)) as connection:
            roots = connection.execute(
                "SELECT root_label, root_name, status FROM scan_roots ORDER BY root_order"
            ).fetchall()
            files = connection.execute(
                "SELECT root_id, file_id, relative_path FROM files ORDER BY root_id, relative_path"
            ).fetchall()

        self.assertEqual([row[0] for row in roots], ["ROOT-001", "ROOT-002"])
        self.assertTrue(all(row[2] == "completed" for row in roots))
        self.assertEqual(len(files), 3)
        same_relative = [row for row in files if row[2].endswith("결과.xlsx")]
        self.assertEqual(len(same_relative), 2)
        self.assertNotEqual(same_relative[0][1], same_relative[1][1])

    # 대용량 집계의 임시 작업도 메모리가 아닌 디스크를 사용하도록 설정했는지 확인한다.
    def test_sqlite_temporary_work_is_disk_backed(self):
        store = _SQLiteStore(
            self.output,
            ScannerConfig(roots=(self.root_a,), output_db=self.output),
        )
        try:
            temp_store = store.connection.execute("PRAGMA temp_store").fetchone()[0]
        finally:
            store.close()

        self.assertEqual(temp_store, 1)

    # 출력 폴더에 쉼표 구분 파일 없이 데이터베이스 하나만 남는지 확인한다.
    def test_creates_only_one_sqlite_output_file(self):
        scan_to_database(
            ScannerConfig(roots=(self.root_a,), output_db=self.output, commit_every=1),
            progress_stream=io.StringIO(),
        )

        self.assertEqual(list(self.output.parent.iterdir()), [self.output])
        self.assertTrue(self.output.read_bytes().startswith(b"SQLite format 3"))

    # 스캔이 끝나기 전에도 발견된 파일이 다른 연결에서 보이는지 확인한다.
    def test_commits_discovered_files_while_scan_is_running(self):
        observed_counts = []

        # 첫 파일 기록 직후 별도 연결에서 누적 건수를 읽는다.
        def observe(snapshot):
            if snapshot.files_scanned == 1 and not observed_counts:
                with closing(sqlite3.connect(self.output)) as connection:
                    observed_counts.append(
                        connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                    )
                    run_status = connection.execute(
                        "SELECT status FROM scan_run WHERE run_id = 1"
                    ).fetchone()[0]
                    self.assertEqual(run_status, "running")

        scan_to_database(
            ScannerConfig(
                roots=(self.root_a,),
                output_db=self.output,
                commit_every=1,
                progress_every_files=1,
                progress_interval_seconds=0,
            ),
            progress_stream=io.StringIO(),
            progress_callback=observe,
        )

        self.assertEqual(observed_counts, [1])

    # 콘솔 진행 정보가 현재 처리 중인 하위 폴더를 표시하는지 확인한다.
    def test_progress_output_shows_current_folder(self):
        output = io.StringIO()

        scan_to_database(
            ScannerConfig(
                roots=(self.root_a,),
                output_db=self.output,
                commit_every=1,
                progress_interval_seconds=0,
                progress_every_files=1,
            ),
            progress_stream=output,
        )

        progress_text = output.getvalue()
        self.assertIn("현재 폴더", progress_text)
        self.assertIn("보고서", progress_text)
        self.assertIn("완료 폴더", progress_text)

    # 실행 상태와 마지막 처리 위치가 데이터베이스에 계속 갱신되는지 확인한다.
    def test_database_keeps_live_progress_state(self):
        captured = []

        # 보고서 폴더 처리 시점의 실행 상태를 별도 연결로 확인한다.
        def observe(snapshot):
            if snapshot.current_folder == "보고서" and not captured:
                with closing(sqlite3.connect(self.output)) as connection:
                    row = connection.execute(
                        """
                        SELECT status, current_root_label, current_folder,
                               folders_discovered, files_scanned, last_heartbeat_at
                        FROM scan_run WHERE run_id = 1
                        """
                    ).fetchone()
                    captured.append(row)

        scan_to_database(
            ScannerConfig(
                roots=(self.root_a,),
                output_db=self.output,
                commit_every=1,
                progress_interval_seconds=0,
            ),
            progress_stream=io.StringIO(),
            progress_callback=observe,
        )

        self.assertEqual(captured[0][0], "running")
        self.assertEqual(captured[0][1], "ROOT-001")
        self.assertEqual(captured[0][2], "보고서")
        self.assertGreaterEqual(captured[0][3], 3)
        self.assertTrue(captured[0][5])

    # 예기치 않은 중단이 발생하면 실패 원인과 마지막 폴더를 남기는지 확인한다.
    def test_records_failure_reason_and_last_folder(self):
        real_scandir = os.scandir

        # 특정 하위 폴더를 열 때 예기치 않은 오류를 발생시킨다.
        def exploding_scandir(path):
            if Path(path).name == "보고서":
                raise RuntimeError("강제 중단 시험")
            return real_scandir(path)

        with mock.patch("readonly_file_scanner.os.scandir", side_effect=exploding_scandir):
            with self.assertRaisesRegex(RuntimeError, "강제 중단 시험"):
                scan_to_database(
                    ScannerConfig(
                        roots=(self.root_a,),
                        output_db=self.output,
                        commit_every=1,
                        progress_interval_seconds=0,
                    ),
                    progress_stream=io.StringIO(),
                )

        row = self._fetchone(
            """
            SELECT status, current_folder, failure_type, failure_message,
                   failure_traceback
            FROM scan_run WHERE run_id = 1
            """
        )
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["current_folder"], "보고서")
        self.assertEqual(row["failure_type"], "RuntimeError")
        self.assertIn("강제 중단 시험", row["failure_message"])
        self.assertIn("RuntimeError", row["failure_traceback"])

    # 사용자가 중단하면 중단 상태와 마지막 폴더를 남기는지 확인한다.
    def test_records_keyboard_interrupt_as_interrupted(self):
        real_scandir = os.scandir

        # 특정 하위 폴더를 열 때 사용자 중단을 흉내 낸다.
        def interrupting_scandir(path):
            if Path(path).name == "보고서":
                raise KeyboardInterrupt()
            return real_scandir(path)

        with mock.patch(
            "readonly_file_scanner.os.scandir", side_effect=interrupting_scandir
        ):
            with self.assertRaises(KeyboardInterrupt):
                scan_to_database(
                    ScannerConfig(
                        roots=(self.root_a,),
                        output_db=self.output,
                        commit_every=1,
                        progress_interval_seconds=0,
                    ),
                    progress_stream=io.StringIO(),
                )

        row = self._fetchone(
            "SELECT status, phase, current_folder FROM scan_run WHERE run_id = 1"
        )
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["phase"], "interrupted")
        self.assertEqual(row["current_folder"], "보고서")

    # 파일 확정 직후 중단돼도 실제 행 수와 진행 집계를 일치시키는지 확인한다.
    def test_interruption_synchronizes_committed_counts_and_root_status(self):
        original_insert = _SQLiteStore.insert_file

        # 파일을 데이터베이스에 확정한 직후 사용자 중단을 발생시킨다.
        def interrupt_after_insert(store, values):
            original_insert(store, values)
            raise KeyboardInterrupt()

        with mock.patch.object(_SQLiteStore, "insert_file", interrupt_after_insert):
            with self.assertRaises(KeyboardInterrupt):
                scan_to_database(
                    ScannerConfig(
                        roots=(self.root_a,),
                        output_db=self.output,
                        commit_every=1,
                        progress_interval_seconds=60,
                        progress_every_files=0,
                    ),
                    progress_stream=io.StringIO(),
                )

        with closing(sqlite3.connect(self.output)) as connection:
            actual_file_count = connection.execute(
                "SELECT COUNT(*) FROM files"
            ).fetchone()[0]
            run = connection.execute(
                "SELECT status, files_scanned FROM scan_run WHERE run_id = 1"
            ).fetchone()
            root_row = connection.execute(
                """
                SELECT status, file_count, folder_count, byte_count
                FROM scan_roots WHERE root_order = 1
                """
            ).fetchone()
            actual_folder_count = connection.execute(
                "SELECT COUNT(*) FROM folders WHERE root_id = 1"
            ).fetchone()[0]
            actual_byte_count = connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM files WHERE root_id = 1"
            ).fetchone()[0]
            final_event = connection.execute(
                "SELECT event_type FROM scan_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0]

        self.assertEqual(actual_file_count, 1)
        self.assertEqual(run, ("interrupted", actual_file_count))
        self.assertEqual(root_row[0], "interrupted")
        self.assertEqual(root_row[1], actual_file_count)
        self.assertEqual(root_row[2], actual_folder_count)
        self.assertEqual(root_row[3], actual_byte_count)
        self.assertEqual(final_event, "scan_interrupted")

    # 데이터베이스 초기화 도중 실패하면 열린 연결을 닫는지 확인한다.
    def test_store_closes_connection_when_initialization_fails(self):
        fake_connection = mock.MagicMock()

        with mock.patch(
            "readonly_file_scanner.sqlite3.connect",
            return_value=fake_connection,
        ), mock.patch.object(
            _SQLiteStore,
            "_create_schema",
            side_effect=RuntimeError("시험용 초기화 오류"),
        ):
            with self.assertRaisesRegex(RuntimeError, "시험용 초기화 오류"):
                _SQLiteStore(
                    self.output,
                    ScannerConfig(roots=(self.root_a,), output_db=self.output),
                )

        fake_connection.close.assert_called_once_with()

    # 확정 주기 전에 중단돼도 이미 찾은 파일과 진행 건수가 서로 맞는지 확인한다.
    def test_interrupt_preserves_pending_rows_and_reconciles_progress(self):
        interrupted_folder = self.root_a / "중단대상"
        interrupted_folder.mkdir()
        (interrupted_folder / "보존.txt").write_text("보존", encoding="utf-8")
        real_scandir = os.scandir

        # 파일 한 건을 돌려준 다음 사용자 중단을 발생시키는 반복자를 만든다.
        class InterruptAfterFirstEntry:
            # 실제 폴더 반복자를 감싸고 첫 항목 반환 여부를 저장한다.
            def __init__(self, iterator):
                self.iterator = iterator
                self.returned_first = False

            # 문맥 관리자 진입 시 감싼 반복자를 그대로 사용한다.
            def __enter__(self):
                return self

            # 문맥 관리자 종료 시 실제 반복자를 닫는다.
            def __exit__(self, exc_type, exc_value, traceback_value):
                self.iterator.close()
                return False

            # 자기 자신을 반복자로 돌려준다.
            def __iter__(self):
                return self

            # 첫 항목 뒤에는 사용자 중단을 발생시킨다.
            def __next__(self):
                if self.returned_first:
                    raise KeyboardInterrupt()
                self.returned_first = True
                return next(self.iterator)

        # 지정한 하위 폴더에서만 중단 반복자를 사용한다.
        def interrupt_after_one_entry(path):
            iterator = real_scandir(path)
            if Path(path).name == "중단대상":
                return InterruptAfterFirstEntry(iterator)
            return iterator

        with mock.patch(
            "readonly_file_scanner.os.scandir", side_effect=interrupt_after_one_entry
        ):
            with self.assertRaises(KeyboardInterrupt):
                scan_to_database(
                    ScannerConfig(
                        roots=(self.root_a,),
                        output_db=self.output,
                        commit_every=10_000,
                        progress_interval_seconds=3_600,
                        progress_every_files=0,
                    ),
                    progress_stream=io.StringIO(),
                )

        with closing(sqlite3.connect(self.output)) as connection:
            file_count = connection.execute(
                "SELECT COUNT(*) FROM files WHERE filename = '보존.txt'"
            ).fetchone()[0]
            run_row = connection.execute(
                "SELECT status, files_scanned FROM scan_run WHERE run_id = 1"
            ).fetchone()
            total_file_count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]

        self.assertEqual(file_count, 1)
        self.assertEqual(run_row[0], "interrupted")
        self.assertEqual(run_row[1], total_file_count)

    # 실패 메시지에 실제 스캔 루트의 절대경로가 남지 않는지 확인한다.
    def test_redacts_absolute_roots_from_failure_details(self):
        real_scandir = os.scandir

        # 하위 폴더 접근 오류에 실제 루트 경로를 포함시킨다.
        def exploding_scandir(path):
            if Path(path).name == "보고서":
                raise RuntimeError(f"접근 실패: {self.root_a}")
            return real_scandir(path)

        with mock.patch("readonly_file_scanner.os.scandir", side_effect=exploding_scandir):
            with self.assertRaises(RuntimeError):
                scan_to_database(
                    ScannerConfig(
                        roots=(self.root_a,),
                        output_db=self.output,
                        commit_every=1,
                        progress_interval_seconds=0,
                    ),
                    progress_stream=io.StringIO(),
                )

        row = self._fetchone(
            "SELECT failure_message, failure_traceback FROM scan_run WHERE run_id = 1"
        )
        self.assertNotIn(str(self.root_a), row["failure_message"])
        self.assertNotIn(str(self.root_a), row["failure_traceback"])
        self.assertIn("<ROOT-001>", row["failure_message"])

    # 경로 해석 전후의 표기가 달라도 입력 경로를 같은 루트로 가리는지 확인한다.
    def test_redacts_configured_root_alias_after_path_resolution(self):
        configured_root = self.base / "RUNNER~1" / ".." / "업무A"
        configured_output = (
            self.base / "OUTPUT~1" / ".." / "결과" / "scan.sqlite3"
        )
        resolved_root = self.root_a.resolve()
        resolved_output = self.output.resolve()
        real_scandir = os.scandir

        # 해석된 실제 폴더를 스캔하되 오류에는 사용자가 입력한 별칭을 포함시킨다.
        def exploding_scandir(path):
            if Path(path).name == "보고서":
                raise RuntimeError(
                    f"접근 실패: {configured_root}; 결과: {configured_output}"
                )
            return real_scandir(path)

        with mock.patch(
            "readonly_file_scanner.validate_scan_paths",
            return_value=((resolved_root,), resolved_output),
        ):
            with mock.patch(
                "readonly_file_scanner.os.scandir", side_effect=exploding_scandir
            ):
                with self.assertRaises(RuntimeError):
                    scan_to_database(
                        ScannerConfig(
                            roots=(configured_root,),
                            output_db=configured_output,
                            commit_every=1,
                            progress_interval_seconds=0,
                        ),
                        progress_stream=io.StringIO(),
                    )

        row = self._fetchone(
            "SELECT failure_message, failure_traceback FROM scan_run WHERE run_id = 1"
        )
        self.assertNotIn(str(configured_root), row["failure_message"])
        self.assertNotIn(str(configured_root), row["failure_traceback"])
        self.assertNotIn(str(configured_output), row["failure_message"])
        self.assertNotIn(str(configured_output), row["failure_traceback"])
        self.assertIn("<ROOT-001>", row["failure_message"])
        self.assertIn("<OUTPUT_DB>", row["failure_message"])

    # 폴더의 직접 및 하위 파일 수와 용량이 올바르게 집계되는지 확인한다.
    def test_materializes_recursive_folder_rollups(self):
        nested = self.root_a / "보고서" / "하위"
        nested.mkdir()
        (nested / "추가.bin").write_bytes(b"12345")

        scan_to_database(
            ScannerConfig(roots=(self.root_a,), output_db=self.output, commit_every=1),
            progress_stream=io.StringIO(),
        )

        with closing(sqlite3.connect(self.output)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT relative_path, direct_file_count, recursive_file_count,
                       direct_size_bytes, recursive_size_bytes, latest_modified_at
                FROM v_folder_summary
                ORDER BY depth, relative_path
                """
            ).fetchall()

        by_path = {row["relative_path"]: row for row in rows}
        self.assertEqual(by_path["."]["recursive_file_count"], 2)
        self.assertEqual(by_path["."]["recursive_size_bytes"], 8)
        self.assertEqual(by_path["보고서"]["direct_file_count"], 1)
        self.assertEqual(by_path["보고서"]["recursive_file_count"], 2)
        self.assertEqual(by_path["보고서/하위"]["recursive_size_bytes"], 5)
        self.assertTrue(by_path["빈폴더"]["latest_modified_at"])

    # 사람이 사용할 조회용 표와 안내문이 데이터베이스에 들어가는지 확인한다.
    def test_creates_expected_tables_views_and_documents(self):
        scan_to_database(
            ScannerConfig(roots=(self.root_a,), output_db=self.output, commit_every=1),
            progress_stream=io.StringIO(),
        )

        with closing(sqlite3.connect(self.output)) as connection:
            objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            documents = {
                row[0]
                for row in connection.execute("SELECT name FROM embedded_documents")
            }

        for table in (
            "scan_run",
            "scan_roots",
            "folders",
            "files",
            "cleanup_candidates",
            "excluded_paths",
            "scan_errors",
            "scan_events",
            "duplicate_groups",
        ):
            self.assertIn(("table", table), objects)
        for view in (
            "v_file_manifest",
            "v_folder_summary",
            "v_empty_folders",
            "v_old_files",
            "v_large_files",
            "v_same_size_candidates",
            "v_confirmed_duplicates",
        ):
            self.assertIn(("view", view), objects)
        self.assertIn("AI_ANALYSIS_PROMPT", documents)
        self.assertIn("DATABASE_GUIDE", documents)

    # 내용이 같은 파일만 실제 중복 묶음으로 확인되는지 점검한다.
    def test_optional_hashing_detects_duplicates_across_roots(self):
        (self.root_a / "다름.bin").write_bytes(b"abd")

        scan_to_database(
            ScannerConfig(
                roots=(self.root_a, self.root_b),
                output_db=self.output,
                hash_mode="duplicates",
                max_hash_size_bytes=1024 * 1024,
                commit_every=1,
                progress_every_files=1,
                progress_interval_seconds=0,
            ),
            progress_stream=io.StringIO(),
        )

        with closing(sqlite3.connect(self.output)) as connection:
            duplicate_groups = connection.execute(
                "SELECT COUNT(*) FROM duplicate_groups"
            ).fetchone()[0]
            duplicate_members = connection.execute(
                "SELECT COUNT(*) FROM v_confirmed_duplicates"
            ).fetchone()[0]
            different = connection.execute(
                "SELECT duplicate_group_id FROM files WHERE relative_path = '다름.bin'"
            ).fetchone()[0]

        self.assertEqual(duplicate_groups, 1)
        self.assertEqual(duplicate_members, 2)
        self.assertIsNone(different)

    # 기본 제외 폴더는 조회하지 않고 제외 사유를 데이터베이스에 남기는지 확인한다.
    def test_default_excludes_are_recorded_immediately(self):
        ignored = self.root_a / ".git"
        ignored.mkdir()
        (ignored / "config").write_text("x", encoding="utf-8")

        scan_to_database(
            ScannerConfig(roots=(self.root_a,), output_db=self.output, commit_every=1),
            progress_stream=io.StringIO(),
        )

        row = self._fetchone(
            "SELECT relative_path, reason FROM excluded_paths WHERE relative_path = '.git'"
        )
        self.assertEqual(row["reason"], "default_exclude")
        self.assertIsNone(
            self._fetchone("SELECT file_id FROM files WHERE relative_path = '.git/config'")
        )


# 명령행에서 여러 폴더와 데이터베이스 경로를 받는 방식을 점검한다.
class CommandLineTests(unittest.TestCase):
    # 위치 인수로 여러 스캔 폴더를 전달할 수 있는지 확인한다.
    def test_main_accepts_multiple_positional_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first = base / "업무1"
            second = base / "업무2"
            output = base / "scan.sqlite3"
            first.mkdir()
            second.mkdir()
            (first / "a.txt").write_text("a", encoding="utf-8")
            (second / "b.txt").write_text("b", encoding="utf-8")

            with mock.patch("sys.stdout", new=io.StringIO()):
                exit_code = main(
                    [
                        str(first),
                        str(second),
                        "--output",
                        str(output),
                        "--progress-interval",
                        "0",
                        "--commit-every",
                        "1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            with closing(sqlite3.connect(output)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)

    # 반복 옵션으로 여러 스캔 폴더를 전달할 수 있는지 확인한다.
    def test_main_accepts_repeated_root_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first = base / "업무1"
            second = base / "업무2"
            output = base / "scan.sqlite3"
            first.mkdir()
            second.mkdir()

            with mock.patch("sys.stdout", new=io.StringIO()):
                exit_code = main(
                    [
                        "--root",
                        str(first),
                        "--root",
                        str(second),
                        "--output",
                        str(output),
                        "--progress-interval",
                        "0",
                    ]
                )

            self.assertEqual(exit_code, 0)

    # 대화형 입력에서는 역슬래시로 끝나는 경로도 명령행 옵션을 삼키지 않는지 확인한다.
    def test_interactive_mode_accepts_root_ending_in_backslash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "업무 자료\\"
            output = base / "scan.sqlite3"
            root.mkdir()
            (root / "보고서.txt").write_text("내용", encoding="utf-8")

            answers = iter((str(root), "", str(output), "n"))
            with mock.patch("builtins.input", side_effect=answers), mock.patch(
                "sys.stdout", new=io.StringIO()
            ):
                exit_code = main(
                    [
                        "--interactive",
                        "--progress-interval",
                        "0",
                        "--commit-every",
                        "1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            with closing(sqlite3.connect(output)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)

    # 대화형 입력 스트림이 끝나면 추적 내용을 노출하지 않고 오류 종료하는지 확인한다.
    def test_interactive_mode_handles_end_of_input(self):
        with mock.patch("builtins.input", side_effect=EOFError), mock.patch(
            "sys.stderr", new=io.StringIO()
        ) as stderr:
            exit_code = main(["--interactive"])

        self.assertEqual(exit_code, 2)
        self.assertIn("입력이 중간에 종료", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    # 대화형 입력 중 사용자가 중단하면 기존 중단 종료값을 돌려주는지 확인한다.
    def test_interactive_mode_handles_keyboard_interrupt(self):
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt), mock.patch(
            "sys.stdout", new=io.StringIO()
        ) as stdout:
            exit_code = main(["--interactive"])

        self.assertEqual(exit_code, 130)
        self.assertIn("사용자가 입력을 중단", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    # 예상하지 못한 실행 오류도 종료값과 오류 문구로 처리하는지 확인한다.
    def test_main_handles_unexpected_exceptions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "업무"
            output = base / "scan.sqlite3"
            root.mkdir()

            with mock.patch(
                "readonly_file_scanner.scan_to_database",
                side_effect=AssertionError("예상하지 못한 오류"),
            ), mock.patch("sys.stdout", new=io.StringIO()), mock.patch(
                "sys.stderr", new=io.StringIO()
            ) as stderr:
                exit_code = main([str(root), "--output", str(output)])

            self.assertEqual(exit_code, 2)
            self.assertIn("예상하지 못한 오류", stderr.getvalue())

    # 스캔 폴더를 하나도 지정하지 않으면 오류 종료값을 돌려주는지 확인한다.
    def test_main_rejects_missing_roots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "scan.sqlite3"

            with mock.patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(["--output", str(output)])

            self.assertEqual(raised.exception.code, 2)

    # 비대화형 실행에서 출력 경로를 생략하면 기존 오류 계약을 유지하는지 확인한다.
    def test_main_rejects_missing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "업무"
            root.mkdir()

            with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    main([str(root)])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("--output", stderr.getvalue())


# 저장소 문서와 한글 역할 주석이 작성 규칙을 따르는지 확인한다.
class RepositoryStyleTests(unittest.TestCase):
    # 사용설명서에 다중 폴더와 데이터베이스 실행법이 들어 있는지 확인한다.
    def test_readme_contains_required_usage_instructions(self):
        readme = Path(__file__).with_name("README.md")
        self.assertTrue(readme.is_file())
        content = readme.read_text(encoding="utf-8")
        for required_text in (
            "py -3 readonly_file_scanner.py",
            "D:\\업무자료1",
            "E:\\업무자료2",
            "--output",
            "scan.sqlite3",
            "--hash-duplicates",
            "run_scanner.bat",
            "v_file_manifest",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, content)

    # 배치파일이 경로를 조립하지 않고 파이썬 대화형 입력에 맡기는지 확인한다.
    def test_batch_file_delegates_path_input_to_python(self):
        batch = Path(__file__).with_name("run_scanner.bat")
        content = batch.read_text(encoding="utf-8-sig")
        self.assertIn("--interactive", content)
        self.assertIn("-X utf8", content)
        self.assertNotIn("ROOT_ARGS", content)
        self.assertNotIn("EnableDelayedExpansion", content)

    # 운영 코드가 데이터베이스 결과 전체를 한꺼번에 가져오지 않는지 확인한다.
    def test_production_code_does_not_fetch_all_database_rows(self):
        source = Path(__file__).with_name("readonly_file_scanner.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".fetchall()", source)

    # 모든 클래스와 함수 앞에 한 줄짜리 한글 역할 주석이 있는지 확인한다.
    def test_every_definition_has_one_line_korean_role_comment(self):
        for filename in ("readonly_file_scanner.py", "test_readonly_file_scanner.py"):
            with self.subTest(filename=filename):
                source_path = Path(__file__).with_name(filename)
                source = source_path.read_text(encoding="utf-8")
                lines = source.splitlines()
                tree = ast.parse(source)

                for node in ast.walk(tree):
                    if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue

                    start_line = min(
                        [node.lineno, *(decorator.lineno for decorator in node.decorator_list)]
                    )
                    previous_index = start_line - 2
                    while previous_index >= 0 and not lines[previous_index].strip():
                        previous_index -= 1

                    self.assertGreaterEqual(
                        previous_index,
                        0,
                        f"{filename}:{start_line} {node.name} 앞에 역할 주석이 없습니다.",
                    )
                    comment = lines[previous_index].strip()
                    self.assertTrue(
                        comment.startswith("# "),
                        f"{filename}:{start_line} {node.name} 앞에 한 줄 주석이 없습니다.",
                    )
                    self.assertRegex(
                        comment,
                        r"[가-힣]",
                        f"{filename}:{start_line} {node.name} 주석에 한글이 없습니다.",
                    )
                    self.assertNotRegex(
                        comment,
                        r"[A-Za-z]",
                        f"{filename}:{start_line} {node.name} 주석에 영문이 들어 있습니다.",
                    )


# 테스트 모듈을 직접 실행할 때 전체 검사를 시작한다.
if __name__ == "__main__":
    unittest.main()
