#!/usr/bin/env python3
"""Build a reproducible source-only tar.zst and matching SHA256SUMS."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tarfile
import tempfile
import tomllib
from io import BytesIO
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
)
ALLOWED_TREES = (
    "src/raspi_recorder",
    "config",
    "docs/user",
    "packaging/systemd",
)
ALLOWED_FILES = (
    "docs/インストール説明書.md",
    "packaging/README.md",
    "packaging/install.sh",
    "packaging/update-recover.py",
)
EXCLUDED_PARTS = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".DS_Store")
FORBIDDEN_NAMES = frozenset({".env", "raspi-recorder.env"})
FORBIDDEN_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".secret",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if sys.version_info < (3, 14):
        raise SystemExit("配布archiveのbuildにはPython 3.14以降が必要です")
    try:
        import compression.zstd  # noqa: F401
    except ImportError as exc:
        raise SystemExit("Python 3.14のcompression.zstdが必要です") from exc

    epoch = _source_date_epoch()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise SystemExit("pyproject.tomlのversionは安定版SemVerが必要です")

    files = _collect_files()
    prefix = f"raspi-recorder-{version}"
    inner_manifest = "".join(
        f"{_sha256(path)}  {name}\n" for name, path in sorted(files.items())
    ).encode("utf-8")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / f"{prefix}.tar.zst"
    checksums_path = output / "SHA256SUMS"
    if any(
        path.exists() or path.is_symlink() for path in (archive_path, checksums_path)
    ):
        raise SystemExit("出力先にarchiveまたはSHA256SUMSがあります。上書きしません")

    archive_temp = _temporary_path(output, ".release-archive-")
    checksums_temp = _temporary_path(output, ".release-checksums-")
    archive_published = False
    checksums_published = False
    try:
        _write_archive(archive_temp, files, prefix, inner_manifest, epoch)
        _verify_archive(archive_temp, files, prefix, inner_manifest)
        archive_digest = _sha256(archive_temp)
        with checksums_temp.open("w", encoding="ascii", newline="\n") as stream:
            stream.write(f"{archive_digest}  {archive_path.name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(archive_temp, archive_path)
        archive_published = True
        os.replace(checksums_temp, checksums_path)
        checksums_published = True
        _fsync_directory(output)
    except BaseException:
        archive_temp.unlink(missing_ok=True)
        checksums_temp.unlink(missing_ok=True)
        if archive_published:
            archive_path.unlink(missing_ok=True)
        if checksums_published:
            checksums_path.unlink(missing_ok=True)
        raise
    print(f"{archive_path}\n{checksums_path}")
    return 0


def _source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        raise SystemExit("SOURCE_DATE_EPOCHを明示してください")
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit("SOURCE_DATE_EPOCHは整数秒で指定してください") from exc
    if value < 0 or value > 2**63 - 1:
        raise SystemExit("SOURCE_DATE_EPOCHが範囲外です")
    return value


def _collect_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for relative in (*ROOT_FILES, *ALLOWED_FILES):
        _add_file(files, relative, ROOT / relative)
    for relative in ALLOWED_TREES:
        directory = ROOT / relative
        if directory.is_symlink() or not directory.is_dir():
            raise SystemExit(f"許可された配布directoryがありません: {relative}")
        found = False
        for candidate in sorted(directory.rglob("*")):
            rel = candidate.relative_to(ROOT)
            if any(part in EXCLUDED_PARTS for part in rel.parts):
                continue
            if candidate.is_symlink():
                raise SystemExit(f"配布treeにsymlinkがあります: {rel}")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise SystemExit(f"配布treeに特殊fileがあります: {rel}")
            if candidate.name in FORBIDDEN_NAMES or candidate.name.endswith(
                FORBIDDEN_SUFFIXES
            ):
                raise SystemExit(f"機密情報またはdatabase候補があります: {rel}")
            if candidate.name.endswith(EXCLUDED_SUFFIXES):
                continue
            _add_file(files, rel.as_posix(), candidate)
            found = True
        if not found:
            raise SystemExit(f"許可された配布directoryが空です: {relative}")
    return files


def _add_file(files: dict[str, Path], relative: str, source: Path) -> None:
    if relative in files:
        raise SystemExit(f"配布pathが重複しています: {relative}")
    try:
        info = source.lstat()
    except OSError as exc:
        raise SystemExit(f"許可fileがありません: {relative}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"許可fileがregular fileではありません: {relative}")
    if source.name in FORBIDDEN_NAMES or source.name.endswith(FORBIDDEN_SUFFIXES):
        raise SystemExit(f"機密情報またはdatabase候補があります: {relative}")
    files[relative] = source


def _write_archive(
    path: Path,
    files: dict[str, Path],
    prefix: str,
    manifest: bytes,
    epoch: int,
) -> None:
    with tarfile.open(
        path, mode="w:zst", format=tarfile.PAX_FORMAT, level=19
    ) as archive:
        for relative, source in sorted(files.items()):
            _add_entry(archive, prefix, relative, source, epoch)
        info = tarfile.TarInfo(f"{prefix}/SHA256SUMS")
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = epoch
        info.mode = 0o644
        info.type = tarfile.REGTYPE
        info.size = len(manifest)
        archive.addfile(info, BytesIO(manifest))


def _add_entry(
    archive: tarfile.TarFile,
    prefix: str,
    relative: str,
    source: Path,
    epoch: int,
) -> None:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"配布fileが通常fileではありません: {relative}")
    member = tarfile.TarInfo(f"{prefix}/{PurePosixPath(relative).as_posix()}")
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mtime = epoch
    # checkout側の実行bitに依存すると同一内容でもarchiveが変わるため、
    # 更新側（update_bundle）の正規化と同じsuffix規則で決める。
    member.mode = 0o755 if source.suffix in {".py", ".sh"} else 0o644
    member.type = tarfile.REGTYPE
    member.size = info.st_size
    with source.open("rb") as stream:
        archive.addfile(member, stream)


def _verify_archive(
    path: Path, files: dict[str, Path], prefix: str, expected_manifest: bytes
) -> None:
    expected = {f"{prefix}/{name}" for name in files} | {f"{prefix}/SHA256SUMS"}
    try:
        with tarfile.open(path, mode="r:zst") as archive:
            members = archive.getmembers()
            if {member.name for member in members} != expected or len(members) != len(
                expected
            ):
                raise SystemExit("archiveのfile listが許可リストと一致しません")
            digests: dict[str, str] = {}
            for member in members:
                if not member.isfile() or member.sparse is not None:
                    raise SystemExit("archiveにregular file以外が含まれています")
                member_relative = member.name.removeprefix(f"{prefix}/")
                expected_mode = (
                    0o755 if Path(member_relative).suffix in {".py", ".sh"} else 0o644
                )
                if member_relative != "SHA256SUMS" and member.mode != expected_mode:
                    raise SystemExit(f"archive member modeが不正です: {member.name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SystemExit(f"archive memberを読めません: {member.name}")
                digest = hashlib.sha256()
                content = bytearray() if member.name.endswith("/SHA256SUMS") else None
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    if content is not None:
                        content.extend(chunk)
                relative = member.name.removeprefix(f"{prefix}/")
                digests[relative] = digest.hexdigest()
                if content is not None and bytes(content) != expected_manifest:
                    raise SystemExit("archive内SHA256SUMSが一致しません")
            for relative, source in files.items():
                if digests.get(relative) != _sha256(source):
                    raise SystemExit(
                        f"archive member checksumが一致しません: {relative}"
                    )
            actual_manifest = "".join(
                f"{digests[name]}  {name}\n" for name in sorted(files)
            ).encode("utf-8")
            if actual_manifest != expected_manifest:
                raise SystemExit("build中にsourceが変更され、SHA256SUMSと一致しません")
    except (OSError, EOFError, tarfile.TarError, ValueError) as exc:
        raise SystemExit("生成したzstd archiveを検証できません") from exc


def _temporary_path(directory: Path, prefix: str) -> Path:
    # mkstempが排他作成したfileをそのまま使う。名前だけ確保して消すと、
    # 再作成までの間にsymlinkを置かれて追従する余地が生まれる。
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=directory)
    os.close(descriptor)
    return Path(name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
