"""Phase 7: zone fingerprinting.

The scenes here differ only in where the moving scatterer is. That is the whole experiment: if
the feature is picking up position rather than vigour of movement, examples of the same place
must land closer together than examples of different places, and the classifier must decline
rather than guess when shown somewhere it was never taught.

Two properties are asserted more carefully than the accuracy is, because they are the ones whose
failure is silent. An empty room must not be assigned a zone — thermal noise normalizes to a
perfectly good unit vector and the nearest centroid to it is always *some* definite place. And a
fingerprint recorded against one access point must be refused on another rather than compared.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from csi.config import Settings
from csi.dsp.preprocess import PreprocessConfig, Preprocessor
from csi.dsp.zones import (
    Binding,
    SampleVectors,
    ZoneClassifier,
    ZoneConfig,
    build_gate,
    build_model,
    cross_validate,
    mask_key,
    sub_windows,
    zone_features,
)
from csi.ring import FrameRing
from csi.synth import Scene, SceneConfig
from csi.zonestore import ZoneStore, ZoneStoreFull

# Three places in one room, as three distances for the moving scatterer. Path length decides
# which subcarriers its reflection reinforces and which it cancels, so these are genuinely
# different channels rather than three recordings of one.
PLACES = {
    "kitchen": {"motion_distance_m": 3.0, "motion_span_m": 1.4},
    "hallway": {"motion_distance_m": 6.5, "motion_span_m": 1.1},
    "bedroom": {"motion_distance_m": 9.5, "motion_span_m": 1.7},
}
UNTAUGHT = {"motion_distance_m": 16.0, "motion_span_m": 2.6}


def capture(
    duration_s: float = 16.0,
    *,
    motion: float = 1.0,
    seed: int = 0,
    rate_hz: float = 40.0,
    **scene_kwargs,
):
    """One example, through the real preprocessing path into a real ring.

    Half the shipped frame rate, because everything downstream resamples to `ZoneConfig.fs`
    (20 Hz) and 40 Hz is still comfortably above the Nyquist limit for a 5 Hz band. Nothing
    under test can tell the difference, and the suite generates a lot of these.
    """
    config = SceneConfig(seed=seed, motion=motion, rate_hz=rate_hz, **scene_kwargs)
    scene = Scene(config)
    pre = Preprocessor(PreprocessConfig())
    ring = FrameRing(config.n_sub, int(duration_s * config.rate_hz * 1.5) + 64)
    for _ in range(int(duration_s * config.rate_hz)):
        frame = scene.next_frame()
        if frame is not None:
            ring.push(pre.process(frame))
    return ring.seconds(duration_s)


def gate_for(windows, config: ZoneConfig) -> np.ndarray:
    profiles = []
    for window in windows:
        valid = np.flatnonzero(window.mask)
        profile = np.full(window.n_sub, np.nan)
        profile[valid] = window.amp[:, valid].mean(axis=0)
        profiles.append(profile)
    return build_gate(np.vstack(profiles), windows[0].mask, config)


def vectors_for(window, gate, config: ZoneConfig) -> np.ndarray:
    got = [
        v
        for v in (zone_features(sub, gate, config) for sub in sub_windows(window, config))
        if v is not None
    ]
    return np.vstack(got) if got else np.zeros((0, 0))


@pytest.fixture(scope="module")
def taught():
    """Four examples of each of three places, plus the gate they share."""
    config = ZoneConfig()
    windows = {
        (place, i): capture(seed=100 + i, **geometry)
        for place, geometry in PLACES.items()
        for i in range(4)
    }
    gate = gate_for(list(windows.values()), config)
    samples = [
        SampleVectors(f"{place}-{i}", place, vectors_for(window, gate, config))
        for (place, i), window in windows.items()
    ]
    return config, gate, samples


# -- the feature ---------------------------------------------------------------------------


def test_examples_of_one_place_land_closer_than_examples_of_different_places(taught):
    """The premise everything else rests on.

    Asserted as a ratio, not an absolute distance. The absolute scale varies between rooms —
    which is exactly why the thresholds in `ZoneConfig` are fractions of the model's own
    separation rather than constants — but the ordering must not. Measured here at about 2.5x
    on these 16 s, 40 Hz captures, and about 3.7x on 20 s at the shipped 80 Hz: more samples
    per resampled window means less interpolation noise in the profile. The bound is set below
    both so that a real regression trips it and a change of test fixture does not.
    """
    _, _, samples = taught
    within, between = [], []
    for a in samples:
        for b in samples:
            if a.sample_id >= b.sample_id:
                continue
            for va in a.vectors:
                for vb in b.vectors:
                    distance = 1.0 - float(va @ vb)
                    (within if a.zone_id == b.zone_id else between).append(distance)

    assert np.median(between) > 2.0 * np.median(within)


def test_every_example_yields_vectors_of_the_same_length(taught):
    """A fixed gate is the point of `build_gate`. Vectors of differing length would not raise —
    `ZoneModel.match` returns None on a mismatch — they would just quietly never match."""
    _, _, samples = taught
    lengths = {sample.vectors.shape[1] for sample in samples if sample.vectors.size}
    assert len(lengths) == 1


def test_a_window_with_a_hole_in_it_is_refused(taught):
    config, gate, _ = taught
    window = capture(seed=1, **PLACES["kitchen"]).seconds(config.window_s)
    assert zone_features(window, gate, config) is not None

    # Excise two seconds from the middle of the analysis window itself. `uniform_resample` will
    # happily draw a straight line across the gap, and the ramp puts power right across the
    # motion band on every subcarrier at once — a confident fingerprint of the interpolator.
    t = window.t_us
    keep = ~((t > t[0] + 2_000_000) & (t < t[0] + 4_000_000))
    holed = type(window)(
        t_us=t[keep],
        amp=window.amp[keep],
        agc=window.agc[keep],
        rssi=window.rssi[keep],
        mask=window.mask,
    )
    assert zone_features(holed, gate, config) is None


# -- the classifier ------------------------------------------------------------------------


def test_taught_places_are_told_apart_under_leave_one_out(taught):
    config, _, samples = taught
    report = cross_validate(samples, config)
    correct = sum(1 for item in report["samples"] if item["verdict"] == "correct")
    scored = sum(1 for item in report["samples"] if item["verdict"] in ("correct", "wrong"))

    assert scored == len(samples)
    assert correct >= 10  # of 12; one outlier is a fair result and the report names which


def test_an_empty_room_is_never_assigned_a_zone(taught):
    """Thermal noise is as unit-length as any other feature vector. Without the rejection
    threshold — and, upstream of it, the motion gate — the nearest centroid to an empty room is
    always some perfectly definite place."""
    config, gate, samples = taught
    model = build_model(samples, binding=None, gate=gate, config=config)

    empty = capture(seed=7, motion=0.0)
    for sub in sub_windows(empty, config):
        features = zone_features(sub, gate, config)
        if features is None:
            continue
        assert model.match(features).state == "unknown"


def test_a_place_that_was_never_taught_reads_as_unknown(taught):
    config, gate, samples = taught
    model = build_model(samples, binding=None, gate=gate, config=config)

    unseen = capture(seed=999, **UNTAUGHT)
    states = []
    for sub in sub_windows(unseen, config):
        features = zone_features(sub, gate, config)
        if features is not None:
            states.append(model.match(features).state)

    assert states
    assert "matched" not in states


def test_two_places_at_the_same_geometry_are_reported_as_indistinguishable():
    """The honest answer when the physics does not separate two zones. More examples cannot fix
    this; moving the node can."""
    config = ZoneConfig()
    geometry = {"motion_distance_m": 5.0, "motion_span_m": 1.2}
    windows = {
        (place, i): capture(seed=300 + i, **geometry)
        for place in ("study", "landing")
        for i in range(4)
    }
    gate = gate_for(list(windows.values()), config)
    samples = [
        SampleVectors(f"{place}-{i}", place, vectors_for(window, gate, config))
        for (place, i), window in windows.items()
    ]

    report = cross_validate(samples, config)
    assert report["indistinguishable"], report["confusion"]
    assert sorted(report["indistinguishable"][0]["zones"]) == ["landing", "study"]


def test_a_zone_with_one_example_is_reported_rather_than_scored(taught):
    """Held out, its zone has no centroid left. Calling that a wrong answer would be a lie
    about the example."""
    config, gate, samples = taught
    trimmed = [s for s in samples if s.zone_id != "kitchen" or s.sample_id == "kitchen-0"]
    report = cross_validate(trimmed, config)

    kitchen = [item for item in report["samples"] if item["zone_id"] == "kitchen"]
    assert [item["verdict"] for item in kitchen] == ["only-sample"]
    assert report["zones"]["kitchen"]["accuracy"] is None


def test_a_single_zone_still_produces_valid_json(taught):
    """With one zone there is no runner-up, so the margin is undefined.

    Encoded as infinity it survives every assertion in Python and then breaks the browser:
    `json.dumps` writes it as the literal `Infinity`, which is not JSON, and `JSON.parse`
    refuses the entire metrics message — every node's presence, breathing and node health with
    it. And one zone is not an exotic state; it is the one every user passes through on the way
    to their second.
    """
    config, gate, samples = taught
    only = [s for s in samples if s.zone_id == "kitchen"]
    model = build_model(only, binding=None, gate=gate, config=config)

    match = model.match(only[0].vectors[0])
    assert match.state == "matched"
    assert match.margin is None

    encoded = json.dumps(match.as_dict(), allow_nan=False)
    assert json.loads(encoded)["margin"] is None


def test_thresholds_follow_the_config_without_rebuilding(taught):
    """The sliders in the UI move `ZoneConfig` in place. Thresholds are derived at match time so
    that takes effect immediately, rather than after re-reading every example from disk."""
    config, gate, samples = taught
    model = build_model(samples, binding=None, gate=gate, config=config)
    before = model.thresholds.copy()

    config.reject_sigma *= 4.0
    try:
        assert np.all(model.thresholds >= before)
        assert np.any(model.thresholds > before)
    finally:
        config.reject_sigma /= 4.0


# -- the live gate and the vote --------------------------------------------------------------


def _binding(window, node_id=1, src_mac="aa:bb:cc:dd:ee:ff") -> Binding:
    return Binding.make(node_id, window.n_sub, window.mask, src_mac)


def test_a_still_room_is_idle_rather_than_classified(taught):
    config, gate, samples = taught
    model = build_model(samples, binding=None, gate=gate, config=config)
    classifier = ZoneClassifier(config)
    window = capture(seed=3, **PLACES["hallway"])

    result = classifier.update(window, model, moving=False, binding=_binding(window))
    assert result["state"] == "idle"
    assert result.get("zone_id") is None


def test_frames_from_a_different_access_point_are_refused_not_classified(taught):
    config, gate, samples = taught
    window = capture(seed=4, **PLACES["bedroom"])
    model = build_model(
        samples, binding=_binding(window, src_mac="aa:aa:aa:aa:aa:aa"), gate=gate, config=config
    )
    classifier = ZoneClassifier(config)

    result = classifier.update(
        window, model, moving=True, binding=_binding(window, src_mac="bb:bb:bb:bb:bb:bb")
    )
    assert result["state"] == "stale"
    assert "access point" in result["reason"]


def test_an_untrained_model_says_so(taught):
    config, _, _ = taught
    window = capture(seed=5, **PLACES["kitchen"])
    result = ZoneClassifier(config).update(
        window, build_model([], binding=None, gate=np.array([]), config=config),
        moving=True, binding=_binding(window),
    )
    assert result["state"] == "untrained"


def test_the_vote_holds_a_zone_through_one_stray_window(taught):
    """Raw per-window labels flip at the metrics rate. The published label should not."""
    config, gate, samples = taught
    model = build_model(samples, binding=None, gate=gate, config=config)
    classifier = ZoneClassifier(config)

    hallway = capture(seed=6, **PLACES["hallway"])
    binding = _binding(hallway)
    published = [
        classifier.update(sub, model, moving=True, binding=binding)["state"]
        for sub in sub_windows(hallway, config)
    ]
    assert published[-1] in ("matched", "settling", "unknown", "ambiguous")

    # Whatever it settled on, one window of a different room must not move it immediately.
    settled = classifier.update(hallway, model, moving=True, binding=binding)
    stray = capture(seed=6, **PLACES["bedroom"])
    after = classifier.update(stray, model, moving=True, binding=binding)
    assert after["zone_id"] == settled["zone_id"]


# -- persistence -----------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return ZoneStore(tmp_path / "zones", ZoneConfig())


def add(store: ZoneStore, zone_id: str, window, *, node_id=1, src_mac="aa:bb:cc:dd:ee:ff"):
    return store.add_sample(
        zone_id,
        window,
        node_id=node_id,
        src_mac=src_mac,
        channel=6,
        motion_median=12.0,
        motion_enter=4.0,
    )


def test_a_stored_example_survives_a_restart(store, tmp_path):
    zone = store.create_zone("Kitchen")
    add(store, zone.id, capture(seed=11, **PLACES["kitchen"]))
    add(store, zone.id, capture(seed=12, **PLACES["kitchen"]))
    before = store.model_for(1, None)

    reopened = ZoneStore(tmp_path / "zones", ZoneConfig())
    reopened.rebuild()
    after = reopened.model_for(1, None)

    assert after.zone_ids == before.zone_ids
    np.testing.assert_allclose(after.centroids, before.centroids, atol=1e-9)


def test_deleting_an_example_moves_the_centroid(store):
    zone = store.create_zone("Kitchen")
    add(store, zone.id, capture(seed=21, **PLACES["kitchen"]))
    second = add(store, zone.id, capture(seed=22, **PLACES["hallway"]))
    before = store.model_for(1, None).centroids.copy()

    assert store.delete_sample(second.id)
    after = store.model_for(1, None).centroids

    assert not np.allclose(before, after)
    assert not (store.samples_dir / f"{second.id}.npz").exists()
    # The feature cache goes too, or it is an orphan counting against the budget forever.
    assert not (store.samples_dir / f"{second.id}.features.npz").exists()


def test_deleting_a_zone_takes_its_examples_with_it(store):
    zone = store.create_zone("Kitchen")
    sample = add(store, zone.id, capture(seed=31, **PLACES["kitchen"]))

    assert store.delete_zone(zone.id)
    assert store.report()["samples"] == []
    assert not (store.samples_dir / f"{sample.id}.npz").exists()


def test_examples_from_different_links_are_kept_apart(store):
    """Two access points is two sets of fingerprints, not one set with noise in it."""
    zone = store.create_zone("Kitchen")
    add(store, zone.id, capture(seed=41, **PLACES["kitchen"]), src_mac="aa:aa:aa:aa:aa:aa")
    add(store, zone.id, capture(seed=42, **PLACES["kitchen"]), src_mac="bb:bb:bb:bb:bb:bb")

    window = capture(seed=43, **PLACES["kitchen"])
    first = store.model_for(1, _binding(window, src_mac="aa:aa:aa:aa:aa:aa"))
    second = store.model_for(1, _binding(window, src_mac="bb:bb:bb:bb:bb:bb"))

    assert first.counts == second.counts  # one example each, not two in one model
    assert not np.allclose(first.centroids, second.centroids)


def test_a_link_with_no_examples_falls_back_so_the_reason_can_be_shown(store):
    """Returning an empty model would make the UI say "no zones taught", which is false and
    unhelpful. The most recent model comes back instead, mismatched on purpose, so the
    classifier can name the access point the examples were actually recorded against."""
    zone = store.create_zone("Kitchen")
    window = capture(seed=51, **PLACES["kitchen"])
    add(store, zone.id, window, src_mac="aa:aa:aa:aa:aa:aa")

    model = store.model_for(1, _binding(window, src_mac="cc:cc:cc:cc:cc:cc"))
    assert model.trained
    assert model.binding.src_mac == "aa:aa:aa:aa:aa:aa"


def test_the_examples_directory_has_a_budget_and_refuses_rather_than_prunes(tmp_path):
    """The budget is checked before writing, so it can be overshot by at most one example —
    which is the right trade for never silently deleting training data."""
    store = ZoneStore(tmp_path / "zones", ZoneConfig(), max_bytes=1)
    zone = store.create_zone("Kitchen")

    add(store, zone.id, capture(seed=61, **PLACES["kitchen"]))
    with pytest.raises(ZoneStoreFull):
        add(store, zone.id, capture(seed=62, **PLACES["kitchen"]))
    assert len(store.report()["samples"]) == 1


def test_a_sample_whose_data_file_vanished_is_dropped_on_load(store, tmp_path):
    zone = store.create_zone("Kitchen")
    sample = add(store, zone.id, capture(seed=71, **PLACES["kitchen"]))
    (store.samples_dir / f"{sample.id}.npz").unlink()

    reopened = ZoneStore(tmp_path / "zones", ZoneConfig())
    assert reopened.report()["samples"] == []


def test_the_feature_cache_is_rebuilt_when_the_gate_moves(store):
    """Cached vectors are keyed by the gate as well as the feature version. Adding an example
    can move the gate, and a vector built under one gate compared against a centroid built under
    another is the misalignment the fixed gate exists to prevent."""
    zone = store.create_zone("Kitchen")
    add(store, zone.id, capture(seed=81, **PLACES["kitchen"]))
    dimension = store.model_for(1, None).centroids.shape[1]

    store.config.gate_quantile = 0.6
    store.rebuild()
    assert store.model_for(1, None).centroids.shape[1] != dimension


# -- through the hub and the API ---------------------------------------------------------------


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path
    s.web_dir = None
    s.record = False
    s.ensure_dirs()
    return s


def test_the_metrics_payload_carries_a_zone_block(settings):
    from csi.hub import Hub
    from csi.protocol import encode_frame

    hub = Hub(settings)
    scene = Scene(SceneConfig(motion=1.0, **PLACES["kitchen"]))
    for _ in range(1200):
        frame = scene.next_frame()
        if frame is not None:
            hub.handle_datagram(encode_frame(frame), 1000.0)

    state = hub.nodes[1]
    metrics = hub.compute_metrics(state, hub.history_for(state))
    assert metrics["zone"]["state"] == "untrained"


def test_the_api_walks_a_zone_through_its_whole_life(settings):
    from fastapi.testclient import TestClient

    from csi.api import create_app

    with TestClient(create_app(settings)) as client:
        created = client.post("/api/zones", json={"name": "Kitchen"}).json()["zone"]
        assert created["id"] == "kitchen"
        assert client.post("/api/zones", json={"name": " "}).status_code == 400

        listed = client.get("/api/zones").json()
        assert [z["id"] for z in listed["zones"]] == ["kitchen"]
        assert listed["capture"] is None

        # No node has sent anything, so there is nothing to take an example off.
        assert client.post("/api/zones/kitchen/samples", json={"node_id": 1}).status_code == 400
        assert client.post("/api/zones/nope/samples", json={"node_id": 1}).status_code == 404
        assert client.post("/api/zones/kitchen/samples", json={}).status_code == 400

        renamed = client.patch("/api/zones/kitchen", json={"name": "Kitchen sink"}).json()
        assert renamed["zone"]["name"] == "Kitchen sink"
        assert client.patch("/api/zones/nope", json={"name": "x"}).status_code == 404

        assert client.delete("/api/zones/kitchen/samples/nope").status_code == 404
        assert client.delete("/api/zones/kitchen").json() == {"ok": True}
        assert client.delete("/api/zones/kitchen").status_code == 404


def test_a_capture_records_an_example_and_flags_a_quiet_one(settings):
    """The capture path end to end: countdown, snapshot off the ring, validation, storage.

    The room is empty throughout, so this also covers the `quiet` flag — an example recorded
    while nobody moved is stored and marked rather than refused, because the person who recorded
    it is the one who knows whether they were moving, and deleting it is the remedy the feature
    is built around.
    """
    from fastapi.testclient import TestClient

    from csi.api import create_app
    from csi.protocol import encode_frame

    settings.metrics_hz = 20.0
    settings.presence.calibration_s = 2.0

    with TestClient(create_app(settings)) as client:
        hub = client.app.state.hub
        scene = Scene(SceneConfig(motion=0.0))
        for _ in range(2400):  # 30 s at 80 Hz, enough to calibrate presence
            frame = scene.next_frame()
            if frame is not None:
                hub.handle_datagram(encode_frame(frame), 1000.0)

        client.post("/api/zones", json={"name": "Kitchen"})
        started = client.post(
            "/api/zones/kitchen/samples",
            json={"node_id": 1, "duration_s": 6, "countdown_s": 0},
        )
        assert started.status_code == 200
        assert started.json()["capture"]["zone_id"] == "kitchen"
        # Only one at a time: recording an example is a physical act.
        assert (
            client.post("/api/zones/kitchen/samples", json={"node_id": 1}).status_code == 409
        )

        for _ in range(300):
            if client.get("/api/zones").json()["capture"] is None:
                break
            time.sleep(0.1)

        body = client.get("/api/zones").json()
        assert body["capture"] is None
        assert len(body["samples"]) == 1
        sample = body["samples"][0]
        assert sample["zone_id"] == "kitchen"
        assert sample["quiet"] is True
        assert sample["motion_median"] < sample["motion_enter"]
        assert sample["duration_s"] >= 4.0

        assert client.delete(f"/api/zones/kitchen/samples/{sample['id']}").json() == {"ok": True}
        assert client.get("/api/zones").json()["samples"] == []


def test_mask_key_distinguishes_masks():
    a = np.array([True, False, True, True])
    assert mask_key(a) == mask_key(a.copy())
    assert mask_key(a) != mask_key(np.array([True, True, True, True]))
