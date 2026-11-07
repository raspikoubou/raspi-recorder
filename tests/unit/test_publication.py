from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "publication",
    Path(__file__).resolve().parents[2] / "packaging/check_publication.py",
)
assert SPEC is not None and SPEC.loader is not None
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def test_commit_email_check_does_not_echo_the_address(self):
        data = "abc\x00someone@example.invalid\x00noreply@github.com\n"
        with patch.object(publication.subprocess, "check_output", return_value=data):
            errors = publication.check_commit_identities(Path("."))
        self.assertTrue(errors)
        self.assertNotIn("someone@example.invalid", "\n".join(errors))
        data = "abc\x00user@users.noreply.github.com\x00noreply@github.com\n"
        with patch.object(publication.subprocess, "check_output", return_value=data):
            self.assertEqual(publication.check_commit_identities(Path(".")), [])

    def test_rejects_private_reports_without_echoing_their_contents(self):
        for text in (
            "17 passed, 3 skipped",
            "11サブテスト成功",
            "例示17件成功",
            "例示用: 未特定",
            "例示用: 未検証",
            "Historical audit: prior wheelhouse",
            "runtime observation",
        ):
            with self.subTest(kind=text.split()[0]):
                errors = publication.check_text("notes", text)
                self.assertTrue(errors)
                self.assertNotIn(text, "\n".join(errors))

    def test_rejects_credentials_and_device_details(self):
        token = "gh" + "p_" + "a" * 36
        for text in (token, "192.168.0." + "9", "/Users/" + "someone/private"):
            errors = publication.check_text("notes", text)
            self.assertTrue(errors)
            self.assertNotIn(text, "\n".join(errors))

    def test_allows_product_instructions_and_required_licenses(self):
        text = (
            "保存単位は5・10・15・20・30・60分です。\nMIT License\n"
            "FFmpegとx264にはGPLの条件が適用されます。"
        )
        self.assertEqual(publication.check_text("manual", text), [])

    def test_rejects_a_tracked_private_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data"
            path.write_text("{}")
            for name in (
                "local/report.md",
                "verification/state.json",
                "state.sqlite3",
                "results.log",
            ):
                self.assertTrue(publication.check_files({name: path}))

    def test_catches_reports_in_source_comments_but_allows_fixture_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_text('# 123 passed\nvalue = "123 passed"\n')
            self.assertTrue(publication.check_files({"src/module.py": path}))
            self.assertEqual(
                publication.check_files({"tests/test_module.py": path}), []
            )


if __name__ == "__main__":
    unittest.main()
