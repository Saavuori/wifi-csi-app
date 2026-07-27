"""Runtime configuration. Environment variables, with defaults that work on a laptop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .dsp.preprocess import PreprocessConfig
from .dsp.presence import PresenceConfig
from .dsp.vitals import VitalsConfig


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    udp_host: str = field(default_factory=lambda: _env("CSI_UDP_HOST", "0.0.0.0"))
    udp_port: int = field(default_factory=lambda: _env_int("CSI_UDP_PORT", 5566))
    http_host: str = field(default_factory=lambda: _env("CSI_HTTP_HOST", "0.0.0.0"))
    http_port: int = field(default_factory=lambda: _env_int("CSI_HTTP_PORT", 8080))

    data_dir: Path = field(
        default_factory=lambda: Path(_env("CSI_DATA_DIR", "./data")).expanduser()
    )
    web_dir: Path | None = field(
        default_factory=lambda: (
            Path(_env("CSI_WEB_DIR", "../web/dist")).expanduser()
            if _env("CSI_WEB_DIR", "../web/dist")
            else None
        )
    )

    # Seconds of history kept in memory per node. Must comfortably exceed the longest analysis
    # window (30 s presence calibration) with room for a slider to be dragged past it.
    history_s: float = field(default_factory=lambda: _env_float("CSI_HISTORY_S", 120.0))
    expected_rate_hz: float = field(default_factory=lambda: _env_float("CSI_RATE_HZ", 80.0))

    # How often derived metrics are computed and pushed. The waterfall gets every frame; the
    # analysis views do not need to, and running an SVD at 80 Hz would be silly.
    metrics_hz: float = field(default_factory=lambda: _env_float("CSI_METRICS_HZ", 5.0))

    # Record every frame by default. Losing a session because recording was off is much more
    # expensive than the disk: a node at 80 Hz writes about 1 GB per day.
    record: bool = field(default_factory=lambda: _env_bool("CSI_RECORD", True))

    # A node that says nothing for this long is reported offline.
    node_timeout_s: float = field(default_factory=lambda: _env_float("CSI_NODE_TIMEOUT_S", 5.0))

    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    breathing: VitalsConfig = field(default_factory=VitalsConfig.breathing)
    heart: VitalsConfig = field(default_factory=VitalsConfig.heart)

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.resolve()
        if self.web_dir is not None:
            self.web_dir = self.web_dir.expanduser().resolve()

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    def ensure_dirs(self) -> None:
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def ring_capacity(self) -> int:
        # Sized from the configured rate with generous headroom, because a node running fast is
        # a much better failure than a history window that silently is not as long as it says.
        return max(512, int(self.history_s * self.expected_rate_hz * 1.5))
