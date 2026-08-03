"""Which subcarriers carry signal, and which are arbitrary values that will look like garbage.

The ESP32 hands over a fixed-size array whose entries include guard bands, DC, and pilots.
Guards and DC are not transmitted at all, so their "CSI" is whatever the FFT happened to leave
in those bins — plotting them produces bright noise at the top and bottom of the waterfall that
is easy to mistake for a real feature.

Index convention: the array index i maps to subcarrier number k as

    k = i            for i < n_sub/2
    k = i - n_sub    otherwise

so index 0 is DC and the negative-frequency half lives in the upper half of the array. This is
the layout esp_wifi uses and the one the tables below assume.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np


@dataclass(frozen=True, slots=True)
class Layout:
    """Active-subcarrier ranges and pilot positions for one bandwidth, in subcarrier numbers."""

    name: str
    n_sub: int
    active: tuple[tuple[int, int], ...]  # inclusive (lo, hi) ranges of k
    pilots: tuple[int, ...]


# 802.11 OFDM subcarrier allocations. The 64/128 entries are what an ESP32 produces in HT20 and
# HT40; 256 is 80 MHz VHT, included so a future Nexmon node drops in without a code change.
LAYOUTS: dict[int, Layout] = {
    64: Layout(
        name="HT20",
        n_sub=64,
        active=((-26, -1), (1, 26)),
        pilots=(-21, -7, 7, 21),
    ),
    128: Layout(
        name="HT40",
        n_sub=128,
        active=((-58, -2), (2, 58)),
        pilots=(-53, -25, -11, 11, 25, 53),
    ),
    256: Layout(
        name="VHT80",
        n_sub=256,
        active=((-122, -2), (2, 122)),
        pilots=(-103, -75, -39, -11, 11, 39, 75, 103),
    ),
}


def index_to_k(n_sub: int) -> np.ndarray:
    """Subcarrier number k for each array index, shape (n_sub,)."""
    i = np.arange(n_sub)
    return np.where(i < n_sub // 2, i, i - n_sub)


@lru_cache(maxsize=16)
def _mask(n_sub: int, drop_pilots: bool, drop_dc_adjacent: bool = False) -> np.ndarray:
    layout = LAYOUTS.get(n_sub)
    k = index_to_k(n_sub)

    if layout is None:
        # Unknown bandwidth: drop DC and a proportional guard band rather than guessing a
        # standard. Better a slightly conservative mask than a confident wrong one.
        guard = max(1, n_sub // 16)
        mask = (np.abs(k) >= 1) & (np.abs(k) <= n_sub // 2 - guard)
    else:
        mask = np.zeros(n_sub, dtype=bool)
        for lo, hi in layout.active:
            mask |= (k >= lo) & (k <= hi)
        if drop_pilots:
            mask &= ~np.isin(k, layout.pilots)

    if drop_dc_adjacent:
        mask &= np.abs(k) != 1

    mask.flags.writeable = False
    return mask


def valid_mask(
    n_sub: int, *, drop_pilots: bool = True, drop_dc_adjacent: bool = False
) -> np.ndarray:
    """Boolean mask of usable subcarriers, shape (n_sub,). Read-only; do not mutate.

    Pilots are dropped by default. They *are* transmitted, so their amplitude is real, but they
    are modulated by a known pseudo-random sign sequence that changes per OFDM symbol; leaving
    them in adds a deterministic flutter that the variance detector reads as motion.

    `drop_dc_adjacent` removes k = +/-1. The standard says those carry data and for the ESP32
    they do, which is why this is off by default. On the Pi's BCM43455 they carry the DC offset
    leaking sideways instead: measured over 150 consecutive frames, index 1 ran 8.2x the median
    amplitude in every single one. That matters beyond looking wrong in the waterfall, because
    subcarrier selection ranks bins by variance to decide where to measure breathing — so the
    one bin carrying no signal is a strong candidate to be chosen first.
    """
    return _mask(n_sub, drop_pilots, drop_dc_adjacent)


def layout_name(n_sub: int) -> str:
    layout = LAYOUTS.get(n_sub)
    return layout.name if layout else f"unknown-{n_sub}"


def describe(
    n_sub: int, *, drop_pilots: bool = True, drop_dc_adjacent: bool = False
) -> dict:
    """Layout summary for the UI, so the waterfall can label its Y axis honestly."""
    mask = valid_mask(n_sub, drop_pilots=drop_pilots, drop_dc_adjacent=drop_dc_adjacent)
    k = index_to_k(n_sub)
    return {
        "n_sub": n_sub,
        "name": layout_name(n_sub),
        "n_valid": int(mask.sum()),
        "valid_indices": np.flatnonzero(mask).tolist(),
        "k": k.tolist(),
    }
