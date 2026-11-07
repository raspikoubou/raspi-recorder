"""Validate and safely stage a Pi-targeted release bundle."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast

from raspi_recorder.ports.updates import ValidatedUpdateBundle

_DEFAULT_ARCHIVE_LIMIT = 512 * 1024 * 1024
_DEFAULT_UNPACKED_LIMIT = 1024 * 1024 * 1024
_DEFAULT_ENTRY_LIMIT = 20_000
_DEFAULT_MANIFEST_LIMIT = 4 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_DECOMPRESSED_OVERHEAD_PER_ENTRY = 1024
_SHA256_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")
_ALLOWED_ROOT_FILES = {
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "SHA256SUMS",
    "docs/インストール説明書.md",
}
_ALLOWED_PACKAGE_TREES = (
    "src/raspi_recorder/",
    "config/",
    "docs/user/",
    "packaging/systemd/",
)
_ALLOWED_PACKAGING_FILES = {
    "packaging/README.md",
    "packaging/install.sh",
    "packaging/update-recover.py",
}


class UpdateBundleError(ValueError):
    """A release archive failed structural, platform, or digest validation."""


@dataclass(frozen=True, slots=True)
class UpdateBundleLimits:
    """Resource ceilings for processing an untrusted release archive."""

    archive_bytes: int = _DEFAULT_ARCHIVE_LIMIT
    unpacked_bytes: int = _DEFAULT_UNPACKED_LIMIT
    entries: int = _DEFAULT_ENTRY_LIMIT
    manifest_bytes: int = _DEFAULT_MANIFEST_LIMIT

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.archive_bytes,
                self.unpacked_bytes,
                self.entries,
                self.manifest_bytes,
            )
        ):
            raise ValueError("bundle limits must be positive integers")


def stage_release_bundle(
    archive_path: Path,
    *,
    expected_version: str,
    destination: Path,
    limits: UpdateBundleLimits | None = None,
    python_version: tuple[int, int] = (3, 13),
    machine: str = "aarch64",
) -> ValidatedUpdateBundle:
    """Verify every bundle member and SHA256SUMS before returning a staged tree.

    Only regular files are supported. Entries are written into a new private
    directory with sanitized permissions; no archive-controlled link or mode is
    ever materialized. Any validation failure removes that staging directory.
    """
    if not isinstance(archive_path, Path) or not isinstance(destination, Path):
        raise ValueError("archive_path and destination must be pathlib Paths")
    version = _stable_version(expected_version)
    limits = limits or UpdateBundleLimits()
    if python_version != (3, 13) or machine != "aarch64":
        raise ValueError("only the supported Pi target Python 3.13/aarch64 is valid")

    archive_fd = _open_regular_archive(archive_path, limits.archive_bytes)
    package_root_name = f"raspi-recorder-{version}"
    try:
        stage_root = _create_stage_root(destination)
        staged = _validate_and_extract(
            archive_fd,
            stage_root,
            package_root_name,
            version,
            limits,
            python_version,
        )
        return staged
    except BaseException:
        if "stage_root" in locals():
            shutil.rmtree(stage_root, ignore_errors=True)
        raise
    finally:
        os.close(archive_fd)


def _stable_version(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("expected_version must be a string")
    candidate = value[1:] if value.startswith("v") else value
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", candidate
    ):
        raise ValueError("expected_version must be a stable SemVer release")
    return candidate


def _open_regular_archive(path: Path, max_bytes: int) -> int:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise UpdateBundleError("release archive cannot be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
            or info.st_size > max_bytes
        ):
            raise UpdateBundleError("release archive size or file type is invalid")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_stage_root(destination: Path) -> Path:
    if not destination.name or destination.name in {".", ".."}:
        raise UpdateBundleError("staging directory path is invalid")
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise UpdateBundleError("staging parent directory does not exist") from exc
    if not parent.is_dir():
        raise UpdateBundleError("staging parent is not a directory")
    root = parent / destination.name
    try:
        os.mkdir(root, 0o700)
    except OSError as exc:
        raise UpdateBundleError(
            "staging directory already exists or cannot be created"
        ) from exc
    return root


def _validate_and_extract(
    archive_fd: int,
    stage_root: Path,
    expected_root: str,
    version: str,
    limits: UpdateBundleLimits,
    python_version: tuple[int, int],
) -> ValidatedUpdateBundle:
    digests: dict[str, str] = {}
    pyproject_data: bytes | None = None
    manifest_data: bytes | None = None
    total_size = 0
    entry_count = 0
    package_root = stage_root / expected_root
    os.mkdir(package_root, 0o700)
    decompressed = stage_root / ".release.tar"
    decompressed_limit = (
        limits.unpacked_bytes
        + limits.entries * _DECOMPRESSED_OVERHEAD_PER_ENTRY
        + limits.manifest_bytes
    )
    try:
        _decompress_zstd(archive_fd, decompressed, decompressed_limit)
        with (
            decompressed.open("rb") as stream,
            cast(Any, tarfile.open)(
                fileobj=stream,
                mode="r|",
                format=tarfile.PAX_FORMAT,
                tarinfo=_limited_tar_info(limits.manifest_bytes),
                stream=True,
            ) as archive,
        ):
            for member in archive:
                entry_count += 1
                if entry_count > limits.entries:
                    raise UpdateBundleError("archive contains too many entries")
                relative = _safe_member_path(member, expected_root)
                _validate_package_allowlist(relative)
                if relative in digests:
                    raise UpdateBundleError("archive contains duplicate paths")
                if not member.isfile() or member.sparse is not None:
                    raise UpdateBundleError("archive contains a link or special entry")
                if type(member.size) is not int or member.size < 0:
                    raise UpdateBundleError("archive member size is invalid")
                total_size += member.size
                if total_size > limits.unpacked_bytes:
                    raise UpdateBundleError("archive expands beyond the size limit")
                if relative in {"pyproject.toml", "SHA256SUMS"} and (
                    member.size > limits.manifest_bytes
                ):
                    raise UpdateBundleError("package metadata exceeds its size limit")

                member_stream = archive.extractfile(member)
                if member_stream is None:
                    raise UpdateBundleError("archive member data cannot be read")
                target = package_root.joinpath(*PurePosixPath(relative).parts)
                _create_safe_parents(package_root, target.parent)
                digest = hashlib.sha256()
                captured = (
                    bytearray()
                    if relative in {"pyproject.toml", "SHA256SUMS"}
                    else None
                )
                written = _copy_member(
                    member_stream, target, member.size, digest, captured
                )
                if written != member.size:
                    raise UpdateBundleError("archive member is truncated")
                digests[relative] = digest.hexdigest()
                if relative == "pyproject.toml":
                    pyproject_data = bytes(captured or b"")
                elif relative == "SHA256SUMS":
                    manifest_data = bytes(captured or b"")
    except UpdateBundleError:
        raise
    except (
        OSError,
        EOFError,
        tarfile.TarError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        raise UpdateBundleError("release archive is corrupt or unreadable") from exc
    finally:
        decompressed.unlink(missing_ok=True)

    if not digests or pyproject_data is None or manifest_data is None:
        raise UpdateBundleError("release archive is missing required files")
    required = {
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "packaging/README.md",
        "packaging/install.sh",
        "packaging/update-recover.py",
        "packaging/systemd/raspi-recorder-engine.service",
        "packaging/systemd/raspi-recorder-web.service",
        "packaging/systemd/raspi-recorder-clock.socket",
        "packaging/systemd/raspi-recorder-clock@.service",
        "packaging/systemd/raspi-recorder-update.socket",
        "packaging/systemd/raspi-recorder-update@.service",
        "packaging/systemd/raspi-recorder-update-recover.service",
        "packaging/systemd/raspi-recorder-engine.service.d/20-clock-helper.conf",
        "packaging/systemd/raspi-recorder-engine.service.d/30-update-helper.conf",
    }
    if not required.issubset(digests):
        raise UpdateBundleError("release archive is missing required package files")
    _verify_project_metadata(pyproject_data, version, python_version)
    _verify_manifest(manifest_data, digests)

    return ValidatedUpdateBundle(
        version=version,
        package_root=package_root,
        file_count=entry_count,
        unpacked_bytes=total_size,
    )


def _decompress_zstd(archive_fd: int, destination: Path, maximum: int) -> None:
    """Fully verify zstd into a private, size-bounded tar stream file."""
    source = os.fdopen(os.dup(archive_fd), "rb")
    process: subprocess.Popen[bytes] | None = None
    try:
        if sys.version_info >= (3, 14):
            from compression import zstd

            decompressed: IO[bytes] = zstd.open(source, "rb")
        else:
            process = subprocess.Popen(
                ["/usr/bin/zstd", "--decompress", "--stdout", "--quiet"],
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            assert process.stdout is not None
            decompressed = process.stdout

        total = 0
        with destination.open("xb") as output, decompressed:
            while True:
                chunk = decompressed.read(min(_CHUNK_SIZE, maximum - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise UpdateBundleError(
                        "archive decompressed stream exceeds the size limit"
                    )
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if process is not None and process.wait(timeout=30) != 0:
            raise UpdateBundleError("release archive is not a valid zstd stream")
    except UpdateBundleError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise UpdateBundleError("release archive is not a valid zstd stream") from exc
    finally:
        source.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


def _limited_tar_info(max_metadata_bytes: int) -> type[tarfile.TarInfo]:
    class LimitedTarInfo(tarfile.TarInfo):
        def _proc_pax(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
            if self.size > max_metadata_bytes:
                raise UpdateBundleError("archive metadata exceeds the size limit")
            processor = getattr(super(), "_proc_pax", None)
            if not callable(processor):
                raise UpdateBundleError("archive PAX metadata is unsupported")
            result = processor(archive)
            if not isinstance(result, tarfile.TarInfo):
                raise UpdateBundleError("archive PAX metadata is invalid")
            return result

        def _proc_gnulong(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
            raise UpdateBundleError("archive contains an unsupported GNU long path")

        def _proc_sparse(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
            raise UpdateBundleError("archive contains a sparse entry")

        def _proc_gnusparse_00(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
            raise UpdateBundleError("archive contains a sparse entry")

        def _proc_gnusparse_01(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
            raise UpdateBundleError("archive contains a sparse entry")

        def _proc_gnusparse_10(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
            raise UpdateBundleError("archive contains a sparse entry")

    return LimitedTarInfo


def _safe_member_path(member: tarfile.TarInfo, expected_root: str) -> str:
    name = member.name
    if not isinstance(name, str) or not name or "\\" in name or name.startswith("/"):
        raise UpdateBundleError("archive contains an unsafe path")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or parts[0] != expected_root:
        raise UpdateBundleError("archive path is outside the versioned package root")
    if len(parts) < 2:
        raise UpdateBundleError("archive contains an invalid root entry")
    return "/".join(parts[1:])


def _validate_package_allowlist(relative: str) -> None:
    if (
        relative in _ALLOWED_ROOT_FILES
        or relative in _ALLOWED_PACKAGING_FILES
        or any(relative.startswith(prefix) for prefix in _ALLOWED_PACKAGE_TREES)
    ):
        return
    raise UpdateBundleError("archive contains a path outside the release allowlist")


def _create_safe_parents(root: Path, parent: Path) -> None:
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise UpdateBundleError("archive path escaped the staging directory") from exc
    current = root
    for component in parent.relative_to(root).parts:
        current = current / component
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise UpdateBundleError(
                    "staging path contains a non-directory"
                ) from None


def _copy_member(
    source: IO[bytes],
    target: Path,
    expected_size: int,
    digest: Any,
    captured: bytearray | None,
) -> int:
    count = 0
    try:
        with target.open("xb") as output:
            while True:
                chunk = source.read(min(_CHUNK_SIZE, expected_size - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > expected_size:
                    raise UpdateBundleError("archive member exceeds its declared size")
                digest.update(chunk)
                if captured is not None:
                    captured.extend(chunk)
                output.write(chunk)
            output.flush()
        target.chmod(
            0o755 if target.suffix == ".py" or target.suffix == ".sh" else 0o644
        )
    except UpdateBundleError:
        raise
    except OSError as exc:
        raise UpdateBundleError("archive member could not be staged") from exc
    return count


def _verify_project_metadata(
    data: bytes, expected_version: str, python_version: tuple[int, int]
) -> None:
    try:
        project = tomllib.loads(data.decode("utf-8"))["project"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise UpdateBundleError("pyproject.toml is invalid") from exc
    if project.get("version") != expected_version:
        raise UpdateBundleError("package version does not match the selected release")
    expected_python = (
        f">={python_version[0]}.{python_version[1]},"
        f"<{python_version[0]}.{python_version[1] + 1}"
    )
    if project.get("requires-python") != expected_python:
        raise UpdateBundleError("package Python requirement differs from the target Pi")


def _verify_manifest(manifest: bytes, archive_digests: dict[str, str]) -> None:
    try:
        lines = manifest.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise UpdateBundleError("SHA256SUMS is not UTF-8") from exc
    listed: dict[str, str] = {}
    for line in lines:
        match = _SHA256_LINE.fullmatch(line)
        if match is None:
            raise UpdateBundleError("SHA256SUMS contains an invalid line")
        digest, name = match.groups()
        _validate_relative_manifest_path(name)
        if name == "SHA256SUMS" or name in listed:
            raise UpdateBundleError(
                "SHA256SUMS contains a duplicate or recursive entry"
            )
        listed[name] = digest
    expected = set(archive_digests) - {"SHA256SUMS"}
    if set(listed) != expected:
        raise UpdateBundleError("SHA256SUMS does not cover the complete package")
    for name, digest in listed.items():
        if archive_digests[name] != digest:
            raise UpdateBundleError(f"SHA256SUMS digest mismatch: {name}")


def _validate_relative_manifest_path(name: str) -> None:
    if not name or "\\" in name or name.startswith("/"):
        raise UpdateBundleError("SHA256SUMS contains an unsafe path")
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise UpdateBundleError("SHA256SUMS contains an unsafe path")


class TarZstdUpdateBundleValidator:
    """Adapter implementing bounded validation for the product tar.zst format."""

    def __init__(self, limits: UpdateBundleLimits | None = None) -> None:
        self._limits = limits or UpdateBundleLimits()

    def stage(
        self,
        archive_path: Path,
        *,
        expected_version: str,
        destination: Path,
    ) -> ValidatedUpdateBundle:
        return stage_release_bundle(
            archive_path,
            expected_version=expected_version,
            destination=destination,
            limits=self._limits,
        )
