"""What build is this, and where did it come from.

Three questions the app should always be able to answer about itself, because the alternative
is asking someone to guess from a running container: which release, which commit, and when it
was built. A node in a cupboard that has been up for a month is exactly where this matters.

Resolution order is deliberate. The build environment wins, because a container knows what it
was built from and the installed package metadata only knows what the version file said at the
time. Package metadata is next, for `pip install -e .` during development. Failing both, this
reports "dev" rather than inventing a number — an unknown build is a fact worth showing, and a
plausible wrong version is worse than an obvious gap.
"""

from __future__ import annotations

import os
from functools import lru_cache

_UNKNOWN = "dev"


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    # Docker's --build-arg leaves the literal placeholder behind when a caller passes nothing,
    # and an image labelled "unknown" reads as a fault rather than as an unset build arg.
    return "" if value in ("", "unknown", "none") else value


@lru_cache(maxsize=1)
def build_info() -> dict[str, str]:
    """Version, commit and build time. Every value is a string; missing ones are empty."""
    version = _env("CSI_VERSION")
    if not version:
        try:
            from importlib.metadata import PackageNotFoundError
            from importlib.metadata import version as pkg_version

            try:
                version = pkg_version("csi-server")
            except PackageNotFoundError:
                version = ""
        except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.8+
            version = ""

    return {
        "version": version or _UNKNOWN,
        "commit": _env("CSI_COMMIT"),
        "built_at": _env("CSI_BUILT_AT"),
    }


def version_string() -> str:
    """One line for a log: `1.4.0 (a1b2c3d)`, degrading to just the version."""
    info = build_info()
    commit = info["commit"][:7]
    return f"{info['version']} ({commit})" if commit else info["version"]
