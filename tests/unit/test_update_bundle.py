import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from raspi_recorder.adapters.update_bundle import (
    UpdateBundleError,
    UpdateBundleLimits,
    stage_release_bundle,
)

VERSION = "1.2.3"
ROOT = f"raspi-recorder-{VERSION}"
PROJECT = b'[project]\nversion = "1.2.3"\nrequires-python = ">=3.13,<3.14"\n'


def bundle_files(
    *,
    project: bytes = PROJECT,
    payload: bytes = b"verified release payload",
    manifest: bytes | None = None,
) -> dict[str, bytes]:
    files = {
        "pyproject.toml": project,
        "README.md": b"release help",
        "LICENSE": b"MIT",
        "THIRD_PARTY_NOTICES.md": b"notice",
        "docs/インストール説明書.md": b"installation guide",
        "packaging/README.md": b"package help",
        "packaging/install.sh": b"#!/bin/sh\n",
        "packaging/update-recover.py": b"print('recover')",
        "packaging/systemd/raspi-recorder-engine.service": b"[Unit]\n",
        "packaging/systemd/raspi-recorder-web.service": b"[Unit]\n",
        "packaging/systemd/raspi-recorder-clock.socket": b"[Socket]\n",
        "packaging/systemd/raspi-recorder-clock@.service": b"[Service]\n",
        "packaging/systemd/raspi-recorder-update.socket": b"[Socket]\n",
        "packaging/systemd/raspi-recorder-update@.service": b"[Service]\n",
        "packaging/systemd/raspi-recorder-update-recover.service": b"[Service]\n",
        "packaging/systemd/"
        "raspi-recorder-engine.service.d/20-clock-helper.conf": b"[Unit]\n",
        "packaging/systemd/"
        "raspi-recorder-engine.service.d/30-update-helper.conf": b"[Unit]\n",
        "src/raspi_recorder/cli.py": payload,
    }
    if manifest is None:
        manifest = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n"
            for name, data in sorted(files.items())
        ).encode("utf-8")
    return {**files, "SHA256SUMS": manifest}


def _zstd_compress(data: bytes) -> bytes:
    try:
        from compression import zstd
    except ImportError as exc:
        if shutil.which("zstd") is None:
            raise RuntimeError(
                "test requires Python 3.14 zstd or the zstd command"
            ) from exc
        return subprocess.run(
            ["zstd", "--quiet", "--stdout"],
            input=data,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
    return zstd.compress(data, level=3)


def write_archive(
    destination: Path,
    files: dict[str, bytes],
    *,
    extra_members: list[tarfile.TarInfo] | None = None,
) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{ROOT}/{name}")
            info.size = len(data)
            info.mode = 0o777
            archive.addfile(info, io.BytesIO(data))
        for member in extra_members or []:
            archive.addfile(member, io.BytesIO(b"link payload"))
    destination.write_bytes(_zstd_compress(raw.getvalue()))
    return destination


class UpdateBundleTests(unittest.TestCase):
    def test_fifo_input_is_rejected_before_a_blocking_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo = root / "release.tar.zst"
            os.mkfifo(fifo)
            script = """
import sys
from pathlib import Path
from raspi_recorder.adapters.update_bundle import (
    stage_release_bundle, UpdateBundleError,
)
from raspi_recorder.adapters.update_helper import UpdateHelper, UpdateHelperError
source, target = map(Path, sys.argv[2:])
try:
    if sys.argv[1] == "validator":
        stage_release_bundle(source, expected_version="1.2.3", destination=target)
    else:
        helper = UpdateHelper(require_root=False)
        helper._copy_verified_archive(source, target, "0" * 64)
except (UpdateBundleError, UpdateHelperError):
    sys.exit(0)
sys.exit("FIFO was accepted")
"""
            for mode in ("validator", "helper"):
                with self.subTest(mode=mode):
                    subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            script,
                            mode,
                            str(fifo),
                            str(root / mode),
                        ],
                        check=True,
                        timeout=3,
                        env={
                            **os.environ,
                            "PYTHONPATH": str(
                                Path(__file__).resolve().parents[2] / "src"
                            ),
                        },
                        capture_output=True,
                    )
                    self.assertFalse((root / mode).exists())

    def test_valid_bundle_is_digest_checked_and_staged_with_sanitized_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = write_archive(root / "release.tar.zst", bundle_files())

            result = stage_release_bundle(
                archive,
                expected_version=VERSION,
                destination=root / "stage",
            )

            self.assertEqual(result.version, VERSION)
            self.assertEqual(result.package_root, (root / "stage" / ROOT).resolve())
            self.assertEqual(result.file_count, 19)
            self.assertFalse((result.package_root / "wheelhouse").exists())
            self.assertEqual(
                (result.package_root / "src/raspi_recorder/cli.py").read_bytes(),
                b"verified release payload",
            )
            self.assertEqual(
                (result.package_root / "packaging/install.sh").stat().st_mode & 0o777,
                0o755,
            )
            self.assertEqual(
                (result.package_root / "README.md").stat().st_mode & 0o777,
                0o644,
            )

    def test_accepts_v_prefixed_stable_release_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = write_archive(root / "release.tar.zst", bundle_files())
            result = stage_release_bundle(
                archive,
                expected_version="v" + VERSION,
                destination=root / "stage",
            )
            self.assertEqual(result.version, VERSION)

    def test_rejects_bad_or_prerelease_expected_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = write_archive(root / "release.tar.zst", bundle_files())
            for version in ("../1.2.3", "1.2.3-rc.1", "01.2.3"):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    stage_release_bundle(
                        archive,
                        expected_version=version,
                        destination=root / f"stage-{version.replace('/', '_')}",
                    )

    def test_rejects_manifest_mismatch_and_missing_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = bundle_files()
            broken = {**original, "src/raspi_recorder/cli.py": b"changed"}
            archive = write_archive(root / "bad-digest.tar.zst", broken)
            with self.assertRaisesRegex(UpdateBundleError, "digest mismatch"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-digest",
                )
            self.assertFalse((root / "stage-digest").exists())

            missing = bundle_files(manifest=b"invalid manifest line\n")
            archive = write_archive(root / "missing-coverage.tar.zst", missing)
            with self.assertRaisesRegex(UpdateBundleError, "invalid line"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-manifest",
                )

    def test_rejects_version_and_python_requirement_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrong_version = PROJECT.replace(b'"1.2.3"', b'"1.2.4"')
            archive = write_archive(
                root / "wrong-version.tar.zst", bundle_files(project=wrong_version)
            )
            with self.assertRaisesRegex(UpdateBundleError, "package version"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-version",
                )

            wrong_python = PROJECT.replace(b"<3.14", b"<3.15")
            archive = write_archive(
                root / "wrong-python.tar.zst", bundle_files(project=wrong_python)
            )
            with self.assertRaisesRegex(UpdateBundleError, "Python requirement"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-python",
                )

    def test_rejects_traversal_absolute_backslash_symlink_and_duplicate_paths(self):
        malicious_names = (
            f"{ROOT}/../escape.txt",
            f"/{ROOT}/absolute.txt",
            f"{ROOT}/wheelhouse\\evil.whl",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, name in enumerate(malicious_names):
                with self.subTest(name=name):
                    archive = root / f"bad-path-{index}.tar.zst"
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    write_archive(archive, bundle_files(), extra_members=[member])
                    with self.assertRaises(UpdateBundleError):
                        stage_release_bundle(
                            archive,
                            expected_version=VERSION,
                            destination=root / f"stage-path-{index}",
                        )

            symlink = tarfile.TarInfo(f"{ROOT}/packaging/systemd/link")
            symlink.type = tarfile.SYMTYPE
            symlink.linkname = "../../etc/passwd"
            archive = write_archive(
                root / "symlink.tar.zst", bundle_files(), extra_members=[symlink]
            )
            with self.assertRaisesRegex(UpdateBundleError, "link or special"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-symlink",
                )

            duplicate = tarfile.TarInfo(f"{ROOT}/README.md")
            duplicate.size = 4
            archive = write_archive(
                root / "duplicate.tar.zst", bundle_files(), extra_members=[duplicate]
            )
            with self.assertRaisesRegex(UpdateBundleError, "duplicate paths"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-duplicate",
                )

    def test_rejects_old_builder_and_wheelhouse_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, relative in enumerate(
                (
                    "packaging/build_release.py",
                    "wheelhouse/av-19.0.1-cp313-cp313-manylinux_aarch64.whl",
                )
            ):
                files = bundle_files()
                files[relative] = b"must not ship"
                manifest = "".join(
                    f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                    for name, data in sorted(files.items())
                    if name != "SHA256SUMS"
                ).encode("utf-8")
                files["SHA256SUMS"] = manifest
                archive = write_archive(root / "rejected.tar.zst", files)
                with (
                    self.subTest(path=relative),
                    self.assertRaisesRegex(
                        UpdateBundleError, "outside the release allowlist"
                    ),
                ):
                    stage_release_bundle(
                        archive,
                        expected_version=VERSION,
                        destination=root / f"stage-{index}",
                    )

    def test_resource_limits_archive_type_and_destination_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = write_archive(root / "release.tar.zst", bundle_files())
            with self.assertRaisesRegex(UpdateBundleError, "size or file type"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-small",
                    limits=UpdateBundleLimits(archive_bytes=1),
                )

            with self.assertRaisesRegex(UpdateBundleError, "expands beyond"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-expanded",
                    limits=UpdateBundleLimits(unpacked_bytes=10),
                )

            existing = root / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(UpdateBundleError, "already exists"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=existing,
                )

            bad_archive = root / "not-archive.tar.zst"
            bad_archive.write_bytes(b"not a zstd archive")
            with self.assertRaisesRegex(UpdateBundleError, "valid zstd stream"):
                stage_release_bundle(
                    bad_archive,
                    expected_version=VERSION,
                    destination=root / "stage-corrupt",
                )

    def test_rejects_decompression_bombs_before_staging_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = bundle_files(payload=b"x" * 1_000_000)
            manifest = "".join(
                f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                for name, data in sorted(files.items())
                if name != "SHA256SUMS"
            ).encode("utf-8")
            files["SHA256SUMS"] = manifest
            archive = write_archive(root / "bomb.tar.zst", files)
            with self.assertRaisesRegex(UpdateBundleError, "decompressed stream"):
                stage_release_bundle(
                    archive,
                    expected_version=VERSION,
                    destination=root / "stage-bomb",
                    limits=UpdateBundleLimits(
                        unpacked_bytes=1024, entries=1, manifest_bytes=64
                    ),
                )
            self.assertFalse((root / "stage-bomb").exists())


if __name__ == "__main__":
    unittest.main()
