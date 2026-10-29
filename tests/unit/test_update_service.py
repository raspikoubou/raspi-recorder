import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from raspi_recorder.adapters.github_releases import release_source_from_environment
from raspi_recorder.adapters.update_bundle import TarZstdUpdateBundleValidator
from raspi_recorder.application.update_service import (
    UpdateService,
    UpdateServiceError,
    UpdateSourceUnconfigured,
)
from raspi_recorder.domain.updates import Release, ReleaseAsset
from tests.unit.test_update_bundle import VERSION, bundle_files, write_archive


def create_release_archive(path: Path) -> Path:
    return write_archive(path, bundle_files())


def release_with_asset(archive: Path, *, tag: str = "v1.2.3") -> Release:
    data = archive.read_bytes()
    asset = ReleaseAsset(
        asset_id=502,
        name=f"raspi-recorder-{VERSION}.tar.zst",
        size=len(data),
        state="uploaded",
        digest=f"sha256:{hashlib.sha256(data).hexdigest()}",
    )
    sums_data = f"{hashlib.sha256(data).hexdigest()}  {asset.name}\n".encode("ascii")
    sums = ReleaseAsset(
        asset_id=503,
        name="SHA256SUMS",
        size=len(sums_data),
        state="uploaded",
        digest=f"sha256:{hashlib.sha256(sums_data).hexdigest()}",
    )
    return Release(
        release_id=501,
        tag_name=tag,
        name="Recorder 1.2.3",
        body="Stable release notes",
        draft=False,
        prerelease=False,
        published_at="2026-10-16T00:00:00Z",
        assets=(asset, sums),
    )


class FakeReleaseSource:
    def __init__(self, archive: Path, releases: tuple[Release, ...]) -> None:
        self.archive = archive
        self.releases = releases
        self.download_calls: list[tuple[int, int, Path]] = []

    def list_releases(self) -> tuple[Release, ...]:
        return self.releases

    def download_asset(
        self,
        release_id: int,
        asset: ReleaseAsset,
        destination: Path,
        *,
        max_bytes: int,
    ) -> str:
        self.download_calls.append((release_id, asset.asset_id, destination))
        if asset.name == "SHA256SUMS":
            archive_digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
            data = f"{archive_digest}  raspi-recorder-{VERSION}.tar.zst\n".encode(
                "ascii"
            )
        else:
            data = self.archive.read_bytes()
        if len(data) > max_bytes or len(data) != asset.size:
            raise ValueError("fake asset size mismatch")
        with destination.open("xb") as output:
            output.write(data)
        return hashlib.sha256(data).hexdigest()


class UpdateServiceTests(unittest.TestCase):
    def test_unconfigured_source_never_attempts_a_network_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = UpdateService(
                current_version="0.1.0.dev0",
                source=None,
                data_directory=Path(temporary),
            )
            self.assertEqual(
                service.status(),
                {
                    "current_version": "0.1.0.dev0",
                    "source_configured": False,
                    "candidate": None,
                    "message": "更新元未設定",
                },
            )
            with self.assertRaises(UpdateSourceUnconfigured):
                service.check()

    def test_check_uses_highest_stable_release_and_exact_asset_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = create_release_archive(root / "candidate.tar.zst")
            stable = release_with_asset(archive)
            draft = Release(
                release_id=600,
                tag_name="v9.0.0",
                name="Draft",
                body="",
                draft=True,
                prerelease=False,
                published_at=None,
                assets=(),
            )
            prerelease = Release(
                release_id=601,
                tag_name="v8.0.0-rc.1",
                name="Pre-release",
                body="",
                draft=False,
                prerelease=True,
                published_at=None,
                assets=(),
            )
            source = FakeReleaseSource(archive, (draft, prerelease, stable))
            service = UpdateService(
                current_version="0.1.0.dev0",
                source=source,
                data_directory=root,
                bundle_validator=TarZstdUpdateBundleValidator(),
            )

            with patch(
                "raspi_recorder.application.update_service.shutil.disk_usage",
                return_value=SimpleNamespace(free=100 * 1024**3),
            ):
                result = service.check()

            self.assertTrue(result["source_configured"])
            self.assertEqual(result["message"], "更新候補があります")
            self.assertEqual(result["candidate"]["version"], VERSION)
            self.assertEqual(result["candidate"]["release_name"], "Recorder 1.2.3")
            self.assertEqual(
                result["candidate"]["asset_size_bytes"], archive.stat().st_size
            )
            self.assertNotIn("url", result["candidate"])
            self.assertEqual(source.download_calls, [])

    def test_check_requires_space_on_both_state_and_install_filesystems(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = create_release_archive(root / "candidate.tar.zst")
            source = FakeReleaseSource(archive, (release_with_asset(archive),))
            service = UpdateService(
                current_version="0.1.0.dev0",
                source=source,
                data_directory=root,
                install_directory=root / "install",
                privileged_state_directory=root / "privileged",
            )
            (root / "install").mkdir()
            (root / "privileged").mkdir()
            with patch(
                "raspi_recorder.application.update_service.shutil.disk_usage",
                side_effect=[
                    SimpleNamespace(free=10 * 1024**3),
                    SimpleNamespace(free=1024**2),
                    SimpleNamespace(free=10 * 1024**3),
                ],
            ):
                result = service.check()

            candidate = result["candidate"]
            self.assertFalse(candidate["enough_space"])
            self.assertEqual(candidate["free_bytes"], 10 * 1024**3)
            self.assertEqual(candidate["install_free_bytes"], 1024**2)

    def test_prepare_rechecks_candidate_downloads_by_asset_id_and_validates_bundle(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = create_release_archive(root / "official.tar.zst")
            source = FakeReleaseSource(archive, (release_with_asset(archive),))
            service = UpdateService(
                current_version="0.1.0.dev0",
                source=source,
                data_directory=root,
                bundle_validator=TarZstdUpdateBundleValidator(),
            )

            with patch(
                "raspi_recorder.application.update_service.shutil.disk_usage",
                return_value=SimpleNamespace(free=100 * 1024**3),
            ):
                prepared = service.prepare(
                    version=VERSION,
                    operation_id="a" * 32,
                )

            self.assertEqual(prepared.candidate.version, VERSION)
            self.assertEqual(prepared.bundle.version, VERSION)
            self.assertTrue(prepared.package_path.is_dir())
            self.assertTrue(
                (root / "update-staging" / ("a" * 32) / "release.tar.zst").exists()
            )
            self.assertEqual(
                [
                    (release_id, asset_id)
                    for release_id, asset_id, _ in source.download_calls
                ],
                [(501, 503), (501, 502)],
            )
            service.cleanup("a" * 32)
            self.assertFalse((root / "update-staging" / ("a" * 32)).exists())

    def test_prepare_fails_closed_when_candidate_changed_or_asset_is_wrong(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = create_release_archive(root / "candidate.tar.zst")
            source = FakeReleaseSource(archive, (release_with_asset(archive),))
            service = UpdateService(
                current_version="0.1.0.dev0",
                source=source,
                data_directory=root,
            )
            with self.assertRaisesRegex(UpdateServiceError, "selected_update_changed"):
                service.prepare(version="1.2.2", operation_id="b" * 32)
            self.assertEqual(source.download_calls, [])

            release = release_with_asset(archive)
            wrong_asset = ReleaseAsset(
                502,
                "something-else.tar.zst",
                release.assets[0].size,
                "uploaded",
                release.assets[0].digest,
            )
            source.releases = (
                Release(
                    release_id=release.release_id,
                    tag_name=release.tag_name,
                    name=release.name,
                    body=release.body,
                    draft=False,
                    prerelease=False,
                    published_at=release.published_at,
                    assets=(wrong_asset,),
                ),
            )
            with self.assertRaisesRegex(UpdateServiceError, "asset_missing"):
                service.prepare(version=VERSION, operation_id="c" * 32)
            self.assertEqual(source.download_calls, [])

    def test_repository_configuration_requires_both_fixed_fields(self):
        source = release_source_from_environment({})
        self.assertEqual(
            (source.owner, source.repository), ("raspikoubou", "raspi-recorder")
        )
        with self.assertRaisesRegex(ValueError, "both update owner"):
            release_source_from_environment({"RASPI_RECORDER_UPDATE_OWNER": "acme"})
        source = release_source_from_environment(
            {
                "RASPI_RECORDER_UPDATE_OWNER": "raspikoubou",
                "RASPI_RECORDER_UPDATE_REPOSITORY": "raspi-recorder",
            }
        )
        self.assertEqual(source.owner, "raspikoubou")
        self.assertEqual(source.repository, "raspi-recorder")
        with self.assertRaisesRegex(ValueError, "must be raspikoubou/raspi-recorder"):
            release_source_from_environment(
                {
                    "RASPI_RECORDER_UPDATE_OWNER": "acme",
                    "RASPI_RECORDER_UPDATE_REPOSITORY": "raspi-recorder",
                }
            )


if __name__ == "__main__":
    unittest.main()
