#!/usr/bin/env python3
"""Write a new version into the files that carry one, and prepend a changelog entry.

Run by .github/workflows/release.yml with the version in the environment. Kept as a script
rather than a pile of `sed` in YAML so that it can be read, and tested, on its own.

The version is edited in place with a targeted regex rather than by parsing and re-emitting
TOML or JSON: a formatter round-trip would reflow files that people also edit by hand, and the
diff of a release commit should be the version and nothing else.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"

HEADER = """# Changelog

Every entry here is written by the release workflow from the pull request that caused it. The
version is decided by that PR's release type — see `.github/workflows/pr-release-type.yml`.

"""


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    """Substitute exactly one match, or fail loudly.

    Zero matches means the file moved on and this script is silently doing nothing; several
    means it is about to rewrite something it was not aiming at. Both have to stop the release,
    because the failure mode otherwise is a tag that disagrees with the package it names.
    """
    original = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, original, count=0, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match for {pattern!r}, found {count}")
    path.write_text(updated, encoding="utf-8")


def commit_lines(previous: str) -> list[str]:
    """Subjects of the commits since the last tag, newest first, minus the release commits."""
    try:
        rev = f"{previous}..HEAD" if previous and previous != "v0.0.0" else "HEAD"
        out = subprocess.run(
            ["git", "log", "--no-merges", "--pretty=format:%s", rev],
            capture_output=True, text=True, check=True, cwd=ROOT,
        ).stdout
    except subprocess.CalledProcessError:
        return []
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip() and not line.startswith("Release v")
    ]


def notes(version: str, release_type: str, previous: str) -> str:
    pr_title = os.environ.get("PR_TITLE", "").strip()
    pr_number = os.environ.get("PR_NUMBER", "").strip()

    lines: list[str] = []
    if pr_title:
        suffix = f" (#{pr_number})" if pr_number else ""
        lines.append(f"{pr_title}{suffix}")
        lines.append("")

    changes = commit_lines(previous)
    if changes:
        lines.append("### Changes")
        lines.extend(f"- {c}" for c in changes)
        lines.append("")

    lines.append(f"Release type: **{release_type}** ({previous} → v{version})")
    return "\n".join(lines).strip() + "\n"


def prepend_changelog(version: str, body: str) -> None:
    entry = f"## v{version} — {date.today().isoformat()}\n\n{body}\n"
    if CHANGELOG.exists():
        existing = CHANGELOG.read_text(encoding="utf-8")
        # Keep the preamble at the top and insert the newest release directly beneath it.
        marker = "\n## "
        index = existing.find(marker)
        if index == -1:
            CHANGELOG.write_text(existing.rstrip() + "\n\n" + entry, encoding="utf-8")
        else:
            CHANGELOG.write_text(
                existing[: index + 1] + entry + existing[index + 1 :], encoding="utf-8"
            )
    else:
        CHANGELOG.write_text(HEADER + entry, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-only", action="store_true",
                        help="print the release notes and change nothing")
    args = parser.parse_args()

    version = os.environ.get("VERSION", "").strip()
    if not version:
        raise SystemExit("VERSION is not set")
    previous = os.environ.get("PREVIOUS", "v0.0.0").strip()
    release_type = os.environ.get("RELEASE_TYPE", "patch").strip()

    body = notes(version, release_type, previous)
    if args.notes_only:
        # Explicitly UTF-8. The notes carry an arrow and commit subjects carry whatever people
        # wrote; on a console defaulting to a legacy codepage the write raises, and it would do
        # so *after* the tag had been pushed — a released version with no release notes.
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):  # pragma: no cover - very old or odd stdout
            pass
        sys.stdout.write(body)
        return 0

    replace_once(
        ROOT / "server" / "pyproject.toml",
        r'^version = "[^"]+"$',
        f'version = "{version}"',
    )
    replace_once(
        ROOT / "web" / "package.json",
        r'^  "version": "[^"]+",$',
        f'  "version": "{version}",',
    )
    prepend_changelog(version, body)
    print(f"version {version} written to pyproject.toml, package.json and CHANGELOG.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
