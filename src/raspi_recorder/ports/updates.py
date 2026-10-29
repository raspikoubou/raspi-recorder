"""Interfaces for reading and validating application update packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from raspi_recorder.domain.updates import Release, ReleaseAsset


class ReleaseSource(Protocol):
    """Trusted, fixed source for stable release metadata and assets."""

    def list_releases(self) -> tuple[Release, ...]:
        """Return a bounded page sequence from the configured release source."""
        ...

    def download_asset(
        self,
        release_id: int,
        asset: ReleaseAsset,
        destination: Path,
        *,
        max_bytes: int,
    ) -> str:
        """Download one release asset and return its computed SHA-256 hex digest."""
        ...


@dataclass(frozen=True, slots=True)
class ValidatedUpdateBundle:
    """Validated package tree prepared for the privileged installation helper."""

    version: str
    package_root: Path
    file_count: int
    unpacked_bytes: int


class UpdateBundleValidator(Protocol):
    """Port for validating and safely staging a release package."""

    def stage(
        self,
        archive_path: Path,
        *,
        expected_version: str,
        destination: Path,
    ) -> ValidatedUpdateBundle:
        """Validate a Pi bundle and return its private staging location."""
        ...
