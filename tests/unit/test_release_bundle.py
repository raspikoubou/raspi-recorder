from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path, PurePosixPath

from raspi_recorder.adapters.update_bundle import stage_release_bundle


@unittest.skipUnless(
    sys.version_info >= (3, 14), "release archive build needs Python 3.14+"
)
class ReleaseBundleTests(unittest.TestCase):
    def test_allowlist_archive_is_deterministic_and_has_no_internal_trees(self):
        root = Path(__file__).resolve().parents[2]
        builder = root / "packaging" / "build_release.py"
        version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
        prefix = f"raspi-recorder-{version}"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = output / "first"
            second = output / "second"
            environment = {**os.environ, "SOURCE_DATE_EPOCH": "0"}
            for target in (first, second):
                subprocess.run(
                    [sys.executable, str(builder), "--output", str(target)],
                    check=True,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
            archive_a = next(first.glob("*.tar.zst"))
            archive_b = next(second.glob("*.tar.zst"))
            self.assertEqual(archive_a.read_bytes(), archive_b.read_bytes())
            checksum_line = (first / "SHA256SUMS").read_text(encoding="ascii").strip()
            digest, name = checksum_line.split("  ", maxsplit=1)
            self.assertEqual(name, archive_a.name)
            self.assertEqual(hashlib.sha256(archive_a.read_bytes()).hexdigest(), digest)
            with tarfile.open(archive_a, "r:zst") as archive:
                members = archive.getmembers()
                names = {member.name for member in members}
                self.assertEqual(len(names), len(members))
                for member in members:
                    path = PurePosixPath(member.name)
                    self.assertFalse(path.is_absolute())
                    self.assertNotIn("..", path.parts)
                    self.assertTrue(member.isfile())
                contents = b"".join(
                    archive.extractfile(member).read()
                    for member in members
                    if member.isfile() and not member.name.endswith("/SHA256SUMS")
                )
            self.assertFalse(any("/local/" in name for name in names))
            self.assertFalse(any("/tests/" in name for name in names))
            self.assertFalse(any("/docs/implementation/" in name for name in names))
            self.assertFalse(any("/docs/requirements/" in name for name in names))
            self.assertFalse(any("/__pycache__/" in name for name in names))
            self.assertFalse(any("/wheelhouse/" in name for name in names))
            self.assertNotIn(b"192.0.2.181", contents)
            self.assertNotIn("SSHパスワード".encode(), contents)
            self.assertNotIn(b"local/verification", contents)
            self.assertTrue(
                any(name.endswith("/docs/user/README.md") for name in names)
            )
            self.assertTrue(
                any(name.endswith("/docs/user/installation.md") for name in names)
            )
            self.assertIn(f"{prefix}/docs/インストール説明書.md", names)
            self.assertIn(
                f"{prefix}/packaging/systemd/raspi-recorder-clock.socket", names
            )
            self.assertIn(
                f"{prefix}/packaging/systemd/raspi-recorder-clock@.service", names
            )
            self.assertIn(
                f"{prefix}/packaging/systemd/raspi-recorder-engine.service.d/20-clock-helper.conf",
                names,
            )
            self.assertIn(f"{prefix}/packaging/update-recover.py", names)
            self.assertIn(f"{prefix}/packaging/install.sh", names)
            self.assertNotIn(f"{prefix}/packaging/build_release.py", names)
            for update_unit in (
                "raspi-recorder-update.socket",
                "raspi-recorder-update@.service",
                "raspi-recorder-update-recover.service",
            ):
                self.assertIn(f"{prefix}/packaging/systemd/{update_unit}", names)
            self.assertIn(
                f"{prefix}/packaging/systemd/raspi-recorder-engine.service.d/30-update-helper.conf",
                names,
            )
            staged = stage_release_bundle(
                archive_a, expected_version=version, destination=output / "staged"
            )
            self.assertEqual(staged.file_count, len(members))
            self.assertEqual(
                (staged.package_root / "LICENSE").read_bytes(),
                (root / "LICENSE").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
