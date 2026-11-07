import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request

from raspi_recorder.adapters.github_releases import (
    GitHubReleaseClient,
    GitHubReleaseError,
)
from raspi_recorder.domain.updates import ReleaseAsset


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "https://api.github.com/repos/acme/recorder/releases",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self._offset = 0
        self.status = status
        self._url = url
        self.headers = headers or {}
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body)
        data = self._body[self._offset : self._offset + size]
        self._offset += len(data)
        return data

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[Request, float]] = []

    def open(self, request: Request, *, timeout: float) -> FakeResponse:
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def release_payload(tag: str = "v1.2.3") -> dict[str, object]:
    return {
        "id": 1,
        "tag_name": tag,
        "name": "Recorder release",
        "body": "Stable update notes",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-16T00:00:00Z",
        "assets": [
            {
                "id": 88,
                "name": "raspi-recorder-1.2.3.tar.zst",
                "size": 2048,
                "state": "uploaded",
                "digest": None,
            }
        ],
    }


class GitHubReleaseClientTests(unittest.TestCase):
    def test_constructor_rejects_url_instead_of_fixed_owner_and_repository(self):
        for owner, repository in (
            ("https://evil.example", "recorder"),
            ("acme", "../recorder"),
        ):
            with self.subTest(owner=owner, repository=repository):
                with self.assertRaises(ValueError):
                    GitHubReleaseClient(owner, repository)

    def test_list_releases_parses_bounded_metadata_and_uses_api_headers(self):
        payload = [release_payload(), {**release_payload("v1.3.0"), "id": 2}]
        opener = FakeOpener([FakeResponse(json.dumps(payload).encode())])
        client = GitHubReleaseClient("acme", "recorder", opener=opener)

        releases = client.list_releases()

        self.assertEqual([release.release_id for release in releases], [1, 2])
        self.assertEqual(releases[0].assets[0].asset_id, 88)
        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/acme/recorder/releases?per_page=100&page=1",
        )
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(request.get_header("X-github-api-version"), "2026-03-10")
        self.assertEqual(timeout, 10)

    def test_list_releases_rejects_invalid_json_shape_and_unexpected_host(self):
        invalid_shape = FakeOpener([FakeResponse(b"{}")])
        with self.assertRaises(GitHubReleaseError):
            GitHubReleaseClient(
                "acme", "recorder", opener=invalid_shape
            ).list_releases()

        wrong_host = FakeOpener(
            [FakeResponse(b"[]", url="https://evil.example/releases")]
        )
        with self.assertRaises(GitHubReleaseError):
            GitHubReleaseClient("acme", "recorder", opener=wrong_host).list_releases()

    def test_list_releases_fails_if_page_limit_could_hide_newer_releases(self):
        payload = [
            {**release_payload(f"v1.{index}.0"), "id": index + 1}
            for index in range(100)
        ]
        opener = FakeOpener([FakeResponse(json.dumps(payload).encode())])
        client = GitHubReleaseClient("acme", "recorder", max_pages=1, opener=opener)
        with self.assertRaisesRegex(GitHubReleaseError, "page limit"):
            client.list_releases()

    def test_rejects_metadata_body_over_limit(self):
        opener = FakeOpener([FakeResponse(b" " * (2 * 1024 * 1024 + 1))])
        with self.assertRaisesRegex(GitHubReleaseError, "size limit"):
            GitHubReleaseClient("acme", "recorder", opener=opener).list_releases()

    def test_download_uses_asset_id_and_verifies_size_and_digest(self):
        data = b"release archive bytes"
        digest = hashlib.sha256(data).hexdigest()
        response = FakeResponse(
            data,
            url="https://release-assets.githubusercontent.com/object",
            headers={"Content-Length": str(len(data))},
        )
        opener = FakeOpener([response])
        asset = ReleaseAsset(
            88,
            "raspi-recorder-1.2.3.tar.zst",
            len(data),
            "uploaded",
            f"sha256:{digest}",
        )
        client = GitHubReleaseClient("acme", "recorder", opener=opener)

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "update.tar.zst"
            result = client.download_asset(7, asset, destination, max_bytes=1024)
            self.assertEqual(result, digest)
            self.assertEqual(destination.read_bytes(), data)

        request, timeout = opener.requests[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/acme/recorder/releases/assets/88",
        )
        self.assertEqual(request.get_header("Accept"), "application/octet-stream")
        self.assertEqual(timeout, 10)
        self.assertTrue(response.closed)

    def test_download_rejects_partial_wrong_digest_and_untrusted_final_url(self):
        asset = ReleaseAsset(88, "bundle.tar.zst", 4, "uploaded", None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            partial = FakeResponse(b"abc")
            client = GitHubReleaseClient(
                "acme", "recorder", opener=FakeOpener([partial])
            )
            destination = root / "partial.tar.zst"
            with self.assertRaisesRegex(GitHubReleaseError, "incomplete"):
                client.download_asset(7, asset, destination, max_bytes=100)
            self.assertFalse(destination.exists())

            wrong_host = FakeResponse(b"data", url="https://evil.example/bundle")
            client = GitHubReleaseClient(
                "acme", "recorder", opener=FakeOpener([wrong_host])
            )
            destination = root / "wrong-host.tar.zst"
            with self.assertRaisesRegex(GitHubReleaseError, "untrusted URL"):
                client.download_asset(7, asset, destination, max_bytes=100)
            self.assertFalse(destination.exists())

            data = b"data"
            wrong_digest_asset = ReleaseAsset(
                88, "bundle.tar.zst", 4, "uploaded", f"sha256:{'0' * 64}"
            )
            client = GitHubReleaseClient(
                "acme", "recorder", opener=FakeOpener([FakeResponse(data)])
            )
            destination = root / "wrong-digest.tar.zst"
            with self.assertRaisesRegex(GitHubReleaseError, "digest"):
                client.download_asset(7, wrong_digest_asset, destination, max_bytes=100)
            self.assertFalse(destination.exists())

    def test_download_rejects_oversize_and_destination_collision(self):
        asset = ReleaseAsset(88, "bundle.tar.zst", 100, "uploaded", None)
        opener = FakeOpener([FakeResponse(b"x" * 100)])
        client = GitHubReleaseClient("acme", "recorder", opener=opener)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bundle.tar.zst"
            with self.assertRaisesRegex(GitHubReleaseError, "size is outside"):
                client.download_asset(7, asset, destination, max_bytes=50)
            destination.write_bytes(b"existing")
            with self.assertRaisesRegex(GitHubReleaseError, "already exists"):
                client.download_asset(7, asset, destination, max_bytes=200)
        self.assertEqual(len(opener.requests), 0)


if __name__ == "__main__":
    unittest.main()
