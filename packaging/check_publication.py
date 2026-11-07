#!/usr/bin/env python3
"""Validate public files and GitHub publication text without echoing their contents."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import subprocess
import tokenize
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PRIVATE_TREES = (
    "local/",
    "verification/",
    "release-prep/",
    "docs/implementation/",
    "docs/requirements/",
    "artifacts/",
    "recordings/",
    ".venv/",
)
_PRIVATE_SUFFIXES = (
    ".log",
    ".jsonl",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db-wal",
    ".db-shm",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".whl",
    ".mp4",
    ".tar.gz",
    ".tar.zst",
)
_DEMO_MEDIA = {
    "docs/demo/media/sample-camera-1.mp4": (
        "b9b51ff003a80e4d936c4f383e9d534a57ca2e5734b02f6b84c0ccf9ad6f8b9d"
    ),
    "docs/demo/media/sample-camera-2.mp4": (
        "2013517e434bb6b98d58118c375f14bea078adca8ab5a170d9d067d05df3f964"
    ),
}

_BINARY_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".ico"}
_PROSE_RULES = (
    ("unresolved investigation", r"未(?:特定|検証|実施|解決)|根本原因.{0,30}不明"),
    ("internal results", r"試験結果|検証結果|検証したコミット|監査指摘"),
    ("result counts", r"\b\d+\s+(?:tests?\s+)?(?:passed|failed|skipped|subtests)\b"),
    ("result counts", r"\d+\s*(?:件|項目)(?:が|は)?(?:成功|失敗|スキップ)"),
    ("result counts", r"\d+\s*サブテスト"),
    (
        "historical investigation",
        r"historical audit|runtime observation|remain unresolved|not retained",
    ),
    ("implementation measurement", r"single-thread path falls below"),
    (
        "internal fixture description",
        r"合成カメラ|隔離した説明用API|能力情報.{0,10}差し替",
    ),
    ("bug inventory", r"(?:既知|未修正).{0,12}(?:バグ|不具合)"),
    (
        "unresolved investigation",
        r"root cause.{0,40}(?:unknown|unidentified)|not (?:yet )?(?:tested|verified)",
    ),
)
_SECRET_RULES = (
    ("private key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ("access token", r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    ("access key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("API key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{25,}|\bAIza[0-9A-Za-z_-]{30,}"),
    ("credential URL", r"https?://[^\s/:]+:[^\s/@]+@"),
    ("personal path", r"/Users/" + r"[^/\s]+/"),
    (
        "device address",
        r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b",
    ),
)


def check_text(label: str, text: str, *, prose: bool = True) -> list[str]:
    """Return locations and rule names, never the rejected content."""
    errors = []
    rules = (*_SECRET_RULES, *_PROSE_RULES) if prose else _SECRET_RULES
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in rules:
            for match in re.finditer(pattern, line, re.IGNORECASE):
                # This documented installation example is not a deployed address.
                if rule == "device address" and match.group() == "192.168.1.50":
                    continue
                errors.append(f"{label}:{number}: {rule}")
                break
    return errors


def _comments(name: str, text: str) -> str:
    if name.endswith(".py"):
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            comments = [t.string for t in tokens if t.type == tokenize.COMMENT]
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    doc = ast.get_docstring(node)
                    if doc:
                        comments.append(doc)
            return "\n".join(comments)
        except (SyntaxError, tokenize.TokenError):
            raise ValueError(f"cannot inspect source: {name}") from None
    return "\n".join(
        line
        for line in text.splitlines()
        if line.lstrip().startswith(("#", "//", "/*", "*"))
    )


def check_files(files: dict[str, Path]) -> list[str]:
    errors = []
    for name, path in sorted(files.items()):
        parts = Path(name).parts
        if name in _DEMO_MEDIA:
            if path.is_symlink() or not path.is_file():
                errors.append(f"{name}: non-regular demo media")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != _DEMO_MEDIA[name]:
                errors.append(f"{name}: unexpected demo media content")
            continue
        if (
            name.startswith(_PRIVATE_TREES)
            or Path(name).name == "AGENTS.md"
            or name.endswith(_PRIVATE_SUFFIXES)
            or Path(name).name.startswith(".env")
            or any(
                p in {"test-results", "test-results.json", "playwright-report"}
                for p in parts
            )
        ):
            errors.append(f"{name}: private artifact")
            continue
        if path.is_symlink() or not path.is_file():
            errors.append(f"{name}: non-regular file")
            continue
        if path.suffix.lower() in _BINARY_IMAGES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"{name}: unexpected binary file")
            continue
        errors.extend(check_text(name, text, prose=False))
        if name.startswith("tests/"):
            # Synthetic input and assertions are public source, not run records.
            if path.suffix in {".md", ".txt"}:
                errors.extend(check_text(name, text))
        elif path.suffix in {".md", ".txt", ".rst"}:
            errors.extend(check_text(name, text))
        elif path.suffix in {".py", ".js", ".sh"}:
            errors.extend(check_text(name + " (comments)", _comments(name, text)))
    return sorted(set(errors))


def repository_files(root: Path) -> dict[str, Path]:
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    )
    return {
        name: root / name
        for name in raw.decode().split("\0")
        if name and ((root / name).exists() or (root / name).is_symlink())
    }


def check_commit_identities(root: Path) -> list[str]:
    lines = subprocess.check_output(
        ["git", "log", "--all", "--format=%H%x00%ae%x00%ce"], cwd=root, text=True
    ).splitlines()
    errors = []
    for line in lines:
        sha, author, committer = line.split("\0")
        if any(
            address != "noreply@github.com"
            and not address.endswith("@users.noreply.github.com")
            for address in (author, committer)
        ):
            errors.append(f"commit {sha[:12]}: personal email metadata")
    return errors


def github_texts(repository: str) -> list[dict[str, str]]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError("invalid repository")
    rows = []
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for endpoint in (
        "releases",
        "issues?state=all",
        "issues/comments",
        "pulls/comments",
    ):
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, 101):
            url = f"https://api.github.com/repos/{repository}/{endpoint}{separator}per_page=100&page={page}"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("metadata response too large")
            items = json.loads(content)
            if not isinstance(items, list):
                raise ValueError("invalid metadata response")
            for item in items:
                rows.append(
                    {
                        "label": item["html_url"],
                        "text": (
                            f"{item.get('title', item.get('name', ''))}\n"
                            f"{item.get('body') or ''}"
                        ),
                    }
                )
            if len(items) < 100:
                break
        else:
            raise ValueError("metadata pagination limit reached")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--text", type=Path, action="append", default=[])
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--github")
    args = parser.parse_args()
    errors = check_files(repository_files(args.root))
    errors.extend(check_commit_identities(args.root))
    for path in args.text:
        errors.extend(check_text(path.name, path.read_text(encoding="utf-8")))
    rows = json.loads(args.metadata.read_text()) if args.metadata else []
    if args.github:
        rows.extend(github_texts(args.github))
    for row in rows:
        errors.extend(check_text(row["label"], row["text"]))
    if errors:
        print("\n".join(sorted(set(errors))))
        return 1
    print("Publication check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
