"""Runtime configuration. Environment variables, with defaults that work on a laptop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .dsp.preprocess import PreprocessConfig
from .dsp.presence import PresenceConfig
from .dsp.vitals import VitalsConfig
from .dsp.zones import ZoneConfig


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

    # Echo port for single-board station nodes configured with CSI_PROBE_UDP_ECHO. Off unless
    # asked for: it is only needed when the node's router will not answer pings, and a port that
    # reflects whatever it is sent should not be open by default.
    echo_port: int = field(default_factory=lambda: _env_int("CSI_ECHO_PORT", 0))

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

    # Cap on the recordings directory, in gigabytes; 0 disables pruning. A node writes roughly
    # a gigabyte a day, and the default home for it is the SD card the operating system is also
    # on. Unbounded, the appliance works for a month and then stops. Oldest recordings are
    # deleted to stay under this — see SessionStore.prune.
    max_disk_gb: float = field(default_factory=lambda: _env_float("CSI_MAX_DISK_GB", 8.0))

    # Age limit on recordings, in hours; 0 disables. What is actually wanted from a monitor left
    # running is the recent past — a recording from last week answers no question anyone asks of
    # it, and on an SD card it costs write endurance to keep. This is the retention rule that
    # matches how the thing is used; `max_disk_gb` stays as a backstop for a node that writes
    # faster than expected.
    max_age_h: float = field(default_factory=lambda: _env_float("CSI_MAX_AGE_H", 24.0))

    # How often the always-on `live` recording is closed and reopened, in hours; 0 disables.
    # Without this, `record` produces one session that is never finished and so can never age
    # out: an age limit keyed on when a recording ended has nothing to key on, and the size
    # prune spares the file being written. Rolling turns the past into finished sessions that
    # retention can treat like any other.
    roll_h: float = field(default_factory=lambda: _env_float("CSI_ROLL_H", 1.0))

    # A node that says nothing for this long is reported offline.
    node_timeout_s: float = field(default_factory=lambda: _env_float("CSI_NODE_TIMEOUT_S", 5.0))

    # Cap on the zone examples directory. Separate from `max_disk_gb` and expressed in megabytes
    # because it is a different kind of thing: recordings are rolling capture that retention is
    # right to delete, while an example someone walked into the kitchen to record is training
    # data. Reaching this budget refuses the next recording rather than quietly deleting an
    # older one. A 20 s example is roughly 0.4 MB at 64 subcarriers and 1.6 MB at 256.
    zones_max_mb: float = field(default_factory=lambda: _env_float("CSI_ZONES_MAX_MB", 200.0))

    preprocess: PreprocessConfig = field(
        default_factory=lambda: PreprocessConfig(
            # Settable from the environment because it is a property of the *hardware* a node
            # uses, not a preference: the BCM43455 leaks DC into k = +/-1 and an ESP32 does
            # not. Left UI-only it was lost on every container restart, which meant the fix
            # had to be reapplied by hand after each deploy and silently was not.
            drop_dc_adjacent=_env_bool("CSI_DROP_DC_ADJACENT", False),
        )
    )
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    breathing: VitalsConfig = field(default_factory=VitalsConfig.breathing)
    heart: VitalsConfig = field(default_factory=VitalsConfig.heart)
    zones: ZoneConfig = field(default_factory=ZoneConfig)

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.resolve()
        if self.web_dir is not None:
            self.web_dir = self.web_dir.expanduser().resolve()

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def zones_dir(self) -> Path:
        return self.data_dir / "zones"

    def ensure_dirs(self) -> None:
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.zones_dir.mkdir(parents=True, exist_ok=True)

    def ring_capacity(self) -> int:
        # Sized from the configured rate with generous headroom, because a node running fast is
        # a much better failure than a history window that silently is not as long as it says.
        return max(512, int(self.history_s * self.expected_rate_hz * 1.5))
