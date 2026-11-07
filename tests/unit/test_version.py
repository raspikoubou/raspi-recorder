"""配布metadataとruntime表示の版一致を保証する。"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from raspi_recorder import __version__


class VersionMetadataTests(unittest.TestCase):
    def test_runtime_version_matches_project_metadata(self) -> None:
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, project["project"]["version"])
