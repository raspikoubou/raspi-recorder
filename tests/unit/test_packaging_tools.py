import configparser
import hashlib
import importlib.util
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_release_under_test", ROOT / "packaging" / "build_release.py"
)
assert SPEC is not None and SPEC.loader is not None
build_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_release)


class BuildReleaseTests(unittest.TestCase):
    def test_runtime_version_matches_project_version(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        module = (ROOT / "src/raspi_recorder/__init__.py").read_text(encoding="utf-8")
        runtime_version = re.search(r'^__version__ = "([^\"]+)"$', module, re.MULTILINE)
        self.assertIsNotNone(runtime_version)
        self.assertEqual(runtime_version.group(1), project["project"]["version"])

    @unittest.skipUnless(
        sys.version_info >= (3, 14), "archive build needs Python 3.14+"
    )
    def test_source_changed_after_manifest_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "README.md"
            source.write_bytes(b"original content")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest = f"{digest}  README.md\n".encode()
            source.write_bytes(b"edited during build")
            archive = Path(directory) / "candidate.tar.zst"
            files = {"README.md": source}
            build_release._write_archive(
                archive, files, "raspi-recorder-1.2.3", manifest, 0
            )
            with self.assertRaisesRegex(SystemExit, "sourceが変更"):
                build_release._verify_archive(
                    archive, files, "raspi-recorder-1.2.3", manifest
                )

    def test_recorder_can_traverse_root_helper_directory_without_write_access(self):
        units = ROOT / "packaging/systemd"
        recovery = configparser.ConfigParser(interpolation=None)
        recovery.read(units / "raspi-recorder-update-recover.service")
        socket = configparser.ConfigParser(interpolation=None)
        socket.read(units / "raspi-recorder-update.socket")
        # The root:root runtime directory must allow the recorder to reach the
        # socket. SocketUser/SocketGroup do not change parent ownership.
        self.assertEqual(recovery["Service"]["User"], "root")
        self.assertEqual(recovery["Service"]["Group"], "root")
        for raw_mode in (
            recovery["Service"]["RuntimeDirectoryMode"],
            socket["Socket"]["DirectoryMode"],
        ):
            mode = int(raw_mode, 8)
            self.assertTrue(mode & 0o001)
            self.assertFalse(mode & 0o022)
        self.assertEqual(socket["Socket"]["SocketGroup"], "raspi-recorder")
        self.assertEqual(int(socket["Socket"]["SocketMode"], 8), 0o660)

    def test_installer_removes_only_expected_venv_lib64_link(self):
        installer = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ -L "$venv_lib64" ]]; then', installer)
        self.assertIn('[[ "$(readlink -- "$venv_lib64")" == "lib" ]]', installer)
        self.assertIn('rm -- "$venv_lib64"', installer)

    def test_installer_relocates_venv_scripts_and_metadata(self):
        installer = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("text.replace(old_root.decode(), new_root.decode())", installer)
        self.assertIn('path.name == "direct_url.json"', installer)
        self.assertIn('venv.rglob("__pycache__")', installer)
        self.assertIn("--no-compile", installer)
        self.assertIn('"$release_path/.venv/bin/raspi-recorder" --version', installer)

    def test_installer_uses_source_runtime_packages(self):
        installer = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        expected = sorted(
            requirement.split("==", 1)[0].lower()
            for requirement in project["project"]["dependencies"]
        )
        option = re.search(r"--no-binary=([a-z0-9,_.-]+)", installer)
        if option is None:
            self.fail("installer must select runtime source distributions")
        self.assertEqual(option.group(1).split(","), expected)
        self.assertNotIn("--no-binary=:all:", installer)

    def test_installer_updates_only_units_managed_by_current_release(self):
        installer = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
        self.assertIn('previous_target="$(readlink -- "$current")"', installer)
        self.assertIn('cmp -s "$previous_unit" "$target"', installer)
        self.assertIn('unit_replacements+=("$unit")', installer)
        self.assertIn("systemctl list-units --state=active", installer)
        self.assertIn('mv -Tf -- "$temp" "$target"', installer)

    def test_release_allowlist_excludes_builders_tests_and_local_data(self):
        files = build_release._collect_files()
        names = set(files)
        self.assertIn("packaging/install.sh", names)
        self.assertIn("docs/インストール説明書.md", names)
        self.assertIn("docs/user/README.md", names)
        self.assertNotIn("packaging/build_release.py", names)
        self.assertFalse(any(name.startswith("wheelhouse/") for name in names))
        self.assertFalse(any(name.startswith("tests/") for name in names))
        self.assertFalse(any(name.startswith("local/") for name in names))
        self.assertFalse(any(name.startswith("docs/requirements/") for name in names))
        self.assertFalse(any(name.endswith(".sqlite3") for name in names))

    def test_illustrated_manuals_and_their_local_links_are_in_release(self):
        files = build_release._collect_files()
        manuals = (
            "docs/user/概要説明書.md",
            "docs/user/インストール説明書.md",
            "docs/user/取扱説明書.md",
        )
        for name in manuals:
            with self.subTest(manual=name):
                self.assertIn(name, files)
                body = files[name].read_text(encoding="utf-8")
                targets = re.findall(r"\]\(([^\s)]+)\)", body)
                targets += re.findall(r'<img\s+src="([^"]+)"', body)
                for target in targets:
                    if target.startswith(("https://", "http://", "#")):
                        continue
                    relative = target.split("#", 1)[0]
                    resolved = (files[name].parent / relative).resolve()
                    packaged = resolved.relative_to(ROOT).as_posix()
                    self.assertIn(packaged, files, (name, target))

    def test_source_date_epoch_must_be_explicit_and_nonnegative(self):
        with patch.dict(build_release.os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "明示"):
                build_release._source_date_epoch()
        with patch.dict(build_release.os.environ, {"SOURCE_DATE_EPOCH": "bad"}):
            with self.assertRaisesRegex(SystemExit, "整数"):
                build_release._source_date_epoch()
        with patch.dict(build_release.os.environ, {"SOURCE_DATE_EPOCH": "-1"}):
            with self.assertRaisesRegex(SystemExit, "範囲外"):
                build_release._source_date_epoch()

    def test_add_file_rejects_symlink_and_special_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(outside)
            with self.assertRaisesRegex(SystemExit, "regular file"):
                build_release._add_file({}, "link.txt", link)


if __name__ == "__main__":
    unittest.main()
