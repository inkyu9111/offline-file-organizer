import ast
import csv
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from readonly_file_scanner import (
    ScannerConfig,
    classify_extension,
    ensure_output_outside_root,
    excel_safe,
    human_size,
    main,
    scan_directory,
    stable_file_id,
    write_reports,
)


# 파일 식별자가 경로 변화에 일관되게 반응하는지 검증한다.
class StableFileIdTests(unittest.TestCase):
    # 대소문자와 경로 구분자가 달라도 같은 식별자가 나오는지 확인한다.
    def test_same_relative_path_has_same_id_case_insensitively(self):
        self.assertEqual(
            stable_file_id("Folder/Report.xlsx"),
            stable_file_id("folder\\report.xlsx"),
        )

    # 서로 다른 경로가 서로 다른 식별자를 만드는지 확인한다.
    def test_different_paths_have_different_ids(self):
        self.assertNotEqual(
            stable_file_id("folder/report.xlsx"),
            stable_file_id("folder/report2.xlsx"),
        )


# 파일 분류와 표시 보조 함수의 동작을 검증한다.
class HelperTests(unittest.TestCase):
    # 업무에서 자주 쓰는 확장자가 올바른 유형으로 분류되는지 확인한다.
    def test_classifies_common_business_extensions(self):
        self.assertEqual(classify_extension(".xlsx"), "spreadsheet")
        self.assertEqual(classify_extension(".hwpx"), "document")
        self.assertEqual(classify_extension(".pptx"), "presentation")
        self.assertEqual(classify_extension(".pdf"), "pdf")
        self.assertEqual(classify_extension(".zip"), "archive")
        self.assertEqual(classify_extension(""), "no_extension")
        self.assertEqual(classify_extension(".unexpected"), "other")

    # 수식처럼 보이는 파일명이 안전하게 표시되는지 확인한다.
    def test_excel_safe_prevents_formula_interpretation(self):
        for value in ("=1+1", "+cmd", "-2+3", "@SUM(A1:A2)", "\tformula"):
            with self.subTest(value=value):
                self.assertTrue(excel_safe(value).startswith("'"))
        self.assertEqual(excel_safe("normal.xlsx"), "normal.xlsx")

    # 바이트 수가 올바른 이진 단위로 표시되는지 확인한다.
    def test_human_size_uses_binary_units(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024), "1.00 KiB")
        self.assertEqual(human_size(1024 * 1024), "1.00 MiB")


# 보고서 경로의 안전 조건을 검증한다.
class OutputSafetyTests(unittest.TestCase):
    # 보고서 폴더가 스캔 대상 안에 있으면 거부하는지 확인한다.
    def test_rejects_output_inside_scan_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "source"
            root.mkdir()
            inside = root / "reports"
            with self.assertRaises(ValueError):
                ensure_output_outside_root(root, inside)

    # 보고서 폴더가 스캔 대상 밖에 있으면 허용하는지 확인한다.
    def test_accepts_output_outside_scan_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "source"
            output = Path(temp_dir) / "reports"
            root.mkdir()
            resolved_root, resolved_output = ensure_output_outside_root(root, output)
            self.assertEqual(resolved_root, root.resolve())
            self.assertEqual(resolved_output, output.resolve())

    # 대상이 없거나 폴더가 아니면 오류를 내는지 확인한다.
    def test_rejects_missing_or_non_directory_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            output = Path(temp_dir) / "reports"
            with self.assertRaises(FileNotFoundError):
                ensure_output_outside_root(missing, output)

            file_root = Path(temp_dir) / "not_a_folder.txt"
            file_root.write_text("x", encoding="utf-8")
            with self.assertRaises(NotADirectoryError):
                ensure_output_outside_root(file_root, output)


# 임시 폴더를 사용해 실제 디렉터리 스캔 동작을 검증한다.
class DirectoryScanTests(unittest.TestCase):
    # 각 시험에 사용할 폴더와 대표 파일을 준비한다.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.output = self.base / "reports"
        self.root.mkdir()

        (self.root / "A").mkdir()
        (self.root / "B").mkdir()
        (self.root / "Empty").mkdir()
        (self.root / ".git").mkdir()

        (self.root / "A" / "report.xlsx").write_bytes(b"abc")
        (self.root / "A" / "report_copy.xlsx").write_bytes(b"abc")
        (self.root / "B" / "other.xlsx").write_bytes(b"abd")
        (self.root / "B" / "notes.txt").write_text("hello", encoding="utf-8")
        (self.root / "B" / "old.tmp").write_bytes(b"tmp")
        (self.root / ".git" / "ignored.txt").write_text("ignored", encoding="utf-8")

        old_epoch = 946684800  # 2000-01-01 UTC
        os.utime(self.root / "B" / "old.tmp", (old_epoch, old_epoch))

    # 시험이 끝나면 임시 폴더를 정리한다.
    def tearDown(self):
        self.temp.cleanup()

    # 원본 변경 여부를 비교할 수 있도록 파일 상태를 기록한다.
    def _snapshot_source(self):
        snapshot = {}
        for path in sorted(self.root.rglob("*"), key=lambda item: str(item).casefold()):
            rel = path.relative_to(self.root).as_posix()
            if path.is_file() and not path.is_symlink():
                stat_result = path.stat()
                snapshot[rel] = ("file", path.read_bytes(), stat_result.st_mtime_ns, stat_result.st_mode)
            elif path.is_dir() and not path.is_symlink():
                stat_result = path.stat()
                snapshot[rel] = ("dir", stat_result.st_mtime_ns, stat_result.st_mode)
            else:
                snapshot[rel] = ("link",)
        return snapshot

    # 기본 제외 규칙을 적용하면서 원본을 바꾸지 않는지 확인한다.
    def test_metadata_scan_excludes_default_noise_and_does_not_change_source(self):
        before = self._snapshot_source()
        config = ScannerConfig(root=self.root, output_base=self.output, hash_mode="none")

        result = scan_directory(config)

        after = self._snapshot_source()
        self.assertEqual(before, after)
        self.assertFalse(self.output.exists(), "scan phase must not create report files")

        relative_paths = [record.relative_path for record in result.files]
        self.assertEqual(
            relative_paths,
            [
                "A/report.xlsx",
                "A/report_copy.xlsx",
                "B/notes.txt",
                "B/old.tmp",
                "B/other.xlsx",
            ],
        )
        self.assertTrue(any(item.relative_path == ".git" for item in result.excluded))
        self.assertTrue(any(folder.relative_path == "Empty" and folder.is_physically_empty for folder in result.folders))
        self.assertTrue(any(candidate.relative_path == "B/old.tmp" for candidate in result.cleanup_candidates))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.files[0].hash_status, "not_requested")

    # 폴더별 직접 및 하위 파일 수와 용량 집계를 확인한다.
    def test_folder_aggregation_counts_recursive_files_and_sizes(self):
        result = scan_directory(ScannerConfig(root=self.root, output_base=self.output, hash_mode="none"))
        by_path = {folder.relative_path: folder for folder in result.folders}
        self.assertEqual(by_path["."].recursive_file_count, 5)
        self.assertEqual(by_path["A"].direct_file_count, 2)
        self.assertEqual(by_path["A"].recursive_size_bytes, 6)
        self.assertEqual(by_path["B"].direct_file_count, 3)
        self.assertEqual(by_path["Empty"].recursive_file_count, 0)

    # 선택적 내용 비교가 실제 중복만 같은 묶음으로 찾는지 확인한다.
    def test_optional_hashing_confirms_only_real_duplicates(self):
        result = scan_directory(
            ScannerConfig(
                root=self.root,
                output_base=self.output,
                hash_mode="duplicates",
                max_hash_size_bytes=1024 * 1024,
            )
        )
        records = {record.relative_path: record for record in result.files}
        first = records["A/report.xlsx"]
        second = records["A/report_copy.xlsx"]
        different = records["B/other.xlsx"]

        self.assertTrue(first.sha256)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.duplicate_group_id, second.duplicate_group_id)
        self.assertTrue(first.duplicate_group_id.startswith("DUP-"))
        self.assertNotEqual(different.duplicate_group_id, first.duplicate_group_id)
        self.assertEqual(len(result.duplicate_groups), 1)
        self.assertEqual(result.duplicate_groups[0].reclaimable_bytes, 3)

    # 사용자 제외 무늬가 상대경로에 적용되는지 확인한다.
    def test_custom_exclude_pattern_applies_to_relative_path(self):
        result = scan_directory(
            ScannerConfig(
                root=self.root,
                output_base=self.output,
                hash_mode="none",
                exclude_patterns=("A/**",),
            )
        )
        self.assertFalse(any(record.relative_path.startswith("A/") for record in result.files))
        self.assertTrue(any(item.relative_path == "A" for item in result.excluded))

    # 일부만 내용 비교된 묶음을 서로 다른 파일로 단정하지 않는지 확인한다.
    def test_same_size_report_does_not_claim_difference_when_group_is_partially_hashed(self):
        isolated_root = self.base / "partial_hash_source"
        isolated_output = self.base / "partial_hash_reports"
        isolated_root.mkdir()
        (isolated_root / "cloud.txt").write_bytes(b"same")
        (isolated_root / "local.txt").write_bytes(b"same")

        # 지정한 파일만 재분석 지점으로 보이도록 시험용 속성을 돌려준다.
        def flags(name, _stat_result):
            return False, False, False, name == "cloud.txt"

        config = ScannerConfig(
            root=isolated_root,
            output_base=isolated_output,
            hash_mode="duplicates",
        )
        with mock.patch("readonly_file_scanner._item_flags", side_effect=flags):
            result = scan_directory(config)
        report_dir = write_reports(result, config)

        with (report_dir / "same_size_candidates.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as source:
            rows = list(csv.DictReader(source))
        by_path = {row["relative_path"]: row for row in rows}
        self.assertEqual(by_path["cloud.txt"]["confirmation_status"], "skipped_reparse_point")
        self.assertEqual(by_path["local.txt"]["confirmation_status"], "partially_hashed_unconfirmed")

    # 파일형 재분석 지점은 목록에 남기고 내용 비교는 건너뛰는지 확인한다.
    def test_regular_file_reparse_point_is_listed_but_not_hashed(self):
        isolated_root = self.base / "reparse_source"
        isolated_output = self.base / "reparse_reports"
        isolated_root.mkdir()
        (isolated_root / "cloud_a.txt").write_bytes(b"same")
        (isolated_root / "cloud_b.txt").write_bytes(b"same")

        with mock.patch(
            "readonly_file_scanner._item_flags",
            return_value=(False, False, False, True),
        ):
            result = scan_directory(
                ScannerConfig(
                    root=isolated_root,
                    output_base=isolated_output,
                    hash_mode="duplicates",
                )
            )

        self.assertEqual(len(result.files), 2)
        self.assertTrue(all(record.is_reparse_point for record in result.files))
        self.assertTrue(all(record.hash_status == "skipped_reparse_point" for record in result.files))
        self.assertEqual(result.duplicate_groups, [])

    # 심볼릭 링크를 따라가지 않고 제외 기록을 남기는지 확인한다.
    def test_symbolic_links_are_not_followed(self):
        target = self.root / "A"
        link = self.root / "link_to_a"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable in this environment")

        result = scan_directory(ScannerConfig(root=self.root, output_base=self.output, hash_mode="none"))
        self.assertTrue(any(item.relative_path == "link_to_a" and item.reason == "link_not_followed" for item in result.excluded))
        self.assertFalse(any(record.relative_path.startswith("link_to_a/") for record in result.files))


# 스캔 결과 보고서 묶음의 생성 형식과 안전성을 검증한다.
class ReportWritingTests(unittest.TestCase):
    # 보고서 생성 시험에 사용할 원본과 결과 폴더를 준비한다.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.output = self.base / "reports"
        self.root.mkdir()
        (self.root / "업무").mkdir()
        (self.root / "업무" / "보고서.xlsx").write_bytes(b"report")
        (self.root / "업무" / "=위험.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.root / "빈폴더").mkdir()

    # 보고서 생성 시험용 임시 자료를 정리한다.
    def tearDown(self):
        self.temp.cleanup()

    # 필수 보고서가 원본 밖에 모두 생성되는지 확인한다.
    def test_writes_complete_report_bundle_outside_source(self):
        config = ScannerConfig(root=self.root, output_base=self.output, hash_mode="none")
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        result = scan_directory(config)

        report_dir = write_reports(result, config)

        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertTrue(report_dir.is_dir())
        self.assertFalse(str(report_dir.resolve()).startswith(str(self.root.resolve())))

        expected = {
            "README_FIRST.txt",
            "inventory_summary.txt",
            "folder_tree.txt",
            "file_manifest.csv",
            "file_manifest.jsonl",
            "folder_summary.csv",
            "empty_folders.csv",
            "large_files.csv",
            "old_files.csv",
            "cleanup_candidates.csv",
            "same_size_candidates.csv",
            "confirmed_duplicates.csv",
            "excluded_paths.csv",
            "scan_errors.csv",
            "scan_metadata.json",
            "AI_ANALYSIS_PROMPT.txt",
        }
        self.assertEqual({path.name for path in report_dir.iterdir()}, expected)
        tree = (report_dir / "folder_tree.txt").read_text(encoding="utf-8-sig")
        self.assertIn("[폴더] 업무", tree)
        self.assertIn("[파일] 보고서.xlsx", tree)
        self.assertIn("F-", tree)

    # 쉼표 구분 파일의 인코딩과 수식 방지, 경로 치환을 확인한다.
    def test_csv_is_utf8_bom_and_formula_safe_without_absolute_paths(self):
        config = ScannerConfig(root=self.root, output_base=self.output, hash_mode="none")
        report_dir = write_reports(scan_directory(config), config)
        manifest_path = report_dir / "file_manifest.csv"

        raw = manifest_path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        decoded = raw.decode("utf-8-sig")
        self.assertIn("'=위험.csv", decoded)
        self.assertNotIn(str(self.base), decoded)

        jsonl = (report_dir / "file_manifest.jsonl").read_text(encoding="utf-8")
        self.assertIn('"filename": "=위험.csv"', jsonl)
        self.assertNotIn(str(self.base), jsonl)

    # 연속 실행해도 서로 다른 보고서 폴더가 만들어지는지 확인한다.
    def test_repeated_writes_create_unique_report_directories(self):
        config = ScannerConfig(root=self.root, output_base=self.output, hash_mode="none")
        result = scan_directory(config)
        first = write_reports(result, config)
        second = write_reports(result, config)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())


# 명령행 실행 흐름과 종료값을 검증한다.
class CliTests(unittest.TestCase):
    # 정상 인수로 실행하면 스캔과 보고서 생성이 끝나는지 확인한다.
    def test_main_scans_and_writes_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "source"
            output = base / "reports"
            root.mkdir()
            (root / "문서.txt").write_text("내용", encoding="utf-8")

            exit_code = main(
                [
                    str(root),
                    "--output",
                    str(output),
                    "--progress-every",
                    "0",
                ]
            )

            self.assertEqual(exit_code, 0)
            report_dirs = [path for path in output.iterdir() if path.is_dir()]
            self.assertEqual(len(report_dirs), 1)
            self.assertTrue((report_dirs[0] / "file_manifest.csv").exists())

    # 위험한 출력 경로를 지정하면 오류 종료값을 돌려주는지 확인한다.
    def test_main_returns_error_when_output_is_inside_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "source"
            root.mkdir()
            (root / "문서.txt").write_text("내용", encoding="utf-8")

            exit_code = main(
                [
                    str(root),
                    "--output",
                    str(root / "reports"),
                    "--progress-every",
                    "0",
                ]
            )

            self.assertEqual(exit_code, 2)
            self.assertFalse((root / "reports").exists())


# 저장소 문서와 한글 역할 주석이 작성 규칙을 따르는지 확인한다.
class RepositoryStyleTests(unittest.TestCase):
    # 사용설명서에 설치와 실행에 필요한 핵심 명령이 들어 있는지 확인한다.
    def test_readme_contains_required_usage_instructions(self):
        readme = Path(__file__).with_name("README.md")
        self.assertTrue(readme.is_file())
        content = readme.read_text(encoding="utf-8")
        for required_text in (
            "py -3 readonly_file_scanner.py",
            "--output",
            "--hash-duplicates",
            "run_scanner.bat",
            "py -3 -m unittest -v test_readonly_file_scanner.py",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, content)

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


if __name__ == "__main__":
    unittest.main()
