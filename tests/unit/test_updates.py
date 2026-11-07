import unittest

from raspi_recorder.domain.updates import (
    Release,
    ReleaseAsset,
    SemanticVersion,
    select_stable_update,
)


def release(
    release_id: int,
    tag: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> Release:
    return Release(
        release_id=release_id,
        tag_name=tag,
        name=tag,
        body="",
        draft=draft,
        prerelease=prerelease,
        published_at="2026-10-16T00:00:00Z",
        assets=(),
    )


class SemanticVersionTests(unittest.TestCase):
    def test_semver_order_uses_numeric_and_prerelease_precedence(self):
        ordered = [
            "1.2.3-alpha.2",
            "1.2.3-alpha.10",
            "1.2.3-beta.1",
            "1.2.3-rc.1",
            "1.2.3",
            "1.2.4",
        ]
        parsed = [SemanticVersion.parse(value) for value in ordered]
        self.assertEqual(parsed, sorted(parsed))

    def test_build_metadata_does_not_change_semver_precedence(self):
        first = SemanticVersion.parse("v1.2.3+one")
        second = SemanticVersion.parse("1.2.3+two")
        self.assertEqual(first, second)
        self.assertFalse(first < second)

    def test_project_dev_version_precedes_next_stable_release(self):
        current = SemanticVersion.from_project_version("0.1.0.dev0")
        stable = SemanticVersion.parse("0.1.0")
        self.assertLess(current, stable)

    def test_rejects_invalid_versions_and_leading_zero_identifiers(self):
        for value in (
            "1.2",
            "01.2.3",
            "1.2.3-01",
            "1.2.3+",
            "1.2.3/asset",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SemanticVersion.parse(value)


class StableReleaseSelectionTests(unittest.TestCase):
    def test_selects_highest_new_stable_and_skips_drafts_prereleases(self):
        selected = select_stable_update(
            (
                release(1, "v0.9.9"),
                release(2, "v1.0.0-rc.1"),
                release(3, "v3.0.0", draft=True),
                release(4, "v2.0.0", prerelease=True),
                release(5, "v1.2.0"),
                release(6, "v1.10.0"),
            ),
            "1.0.0",
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.release_id, 6)

    def test_no_new_stable_release_returns_none(self):
        selected = select_stable_update(
            (
                release(1, "1.0.0"),
                release(2, "1.1.0-rc.1"),
                release(3, "2.0.0", draft=True),
            ),
            "1.0.0",
        )
        self.assertIsNone(selected)

    def test_ambiguous_duplicate_semver_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "same SemVer"):
            select_stable_update(
                (release(1, "v1.1.0"), release(2, "1.1.0")),
                "1.0.0",
            )

    def test_release_metadata_rejects_wrong_types(self):
        with self.assertRaises(ValueError):
            Release(1, "1.0.0", "", "", 0, False, None, ())


class ReleaseAssetTests(unittest.TestCase):
    def test_release_asset_accepts_valid_digest_and_values(self):
        asset = ReleaseAsset(
            asset_id=1,
            name="raspi-recorder-1.0.0.tar.zst",
            size=1024,
            state="uploaded",
            digest=f"sha256:{'a' * 64}",
        )
        self.assertEqual(asset.asset_id, 1)

    def test_release_asset_rejects_boolean_ids_and_invalid_digest(self):
        with self.assertRaises(ValueError):
            ReleaseAsset(True, "bundle.tar.zst", 1, "uploaded", None)
        with self.assertRaises(ValueError):
            ReleaseAsset(1, "bundle.tar.zst", 1, "uploaded", "sha256:bad")


if __name__ == "__main__":
    unittest.main()
