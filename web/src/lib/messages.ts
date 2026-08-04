// The shapes crossing the worker boundary and the WebSocket, in one place.

export interface NodeHealth {
  node_id: number;
  online: boolean;
  rate_hz: number;
  jitter_ms: number;
  frames: number;
  missing: number;
  gaps: number;
  loss_rate: number;
  reorders: number;
  reboots: number;
  /** Re-associations. On a mesh, the count of times the node changed access point mid-session. */
  roams: number;
  /** MAC the frames were transmitted from: the access point's BSSID for a station node. */
  src_mac: string;
  link_epoch: number;
  bad_packets: number;
  rssi: number;
  noise_floor: number;
  /** null when the driver reports no noise floor — brcmfmac on the Pi does not. */
  snr_db: number | null;
  channel: number;
  n_sub: number;
  uptime_s: number;
  last_seen: number;
}

export interface PresenceState {
  state: "calibrating" | "absent" | "present";
  motion: number;
  enter: number;
  exit: number;
  calibration: number;
  selected: number[];
  since_s: number;
}

export interface VitalsState {
  bpm: number;
  /**
   * Share of in-band energy in the peak. Kept because it describes the spectrum, but it is not
   * a trust score: measured against a known signal on real hardware it ran *higher* for noise
   * (0.47) than for a correct detection (0.36). Judge an estimate by `stability_sd`.
   */
  confidence: number;
  /** Spread of the recent run of estimates, in BPM, and the tolerance it had to meet. */
  stability_sd: number;
  stability_tolerance: number;
  snr_db: number;
  band: [number, number];
  window_s: number;
  subcarriers: number[];
  freqs: number[];
  spectrum: number[];
  waveform: number[];
  waveform_fs: number;
}

export interface SelectionState {
  metric: string;
  indices: number[];
  scores: number[];
  gated_out: number[];
}

/**
 * Which taught zone the current movement looks like.
 *
 * Six of the seven states are not a zone, and that is the point — a classifier that always names
 * somewhere is right by chance. `idle` means nothing is moving, `untrained` means no examples
 * exist, `stale` means the link has changed since they were recorded, `settling` means the vote
 * has not converged, `unknown` means it matches nothing taught, and `ambiguous` means two zones
 * are too close to call apart. `reason` carries the explanation for the first three.
 */
export interface ZoneState {
  state: "matched" | "unknown" | "ambiguous" | "idle" | "settling" | "untrained" | "stale";
  zone_id?: string | null;
  distance?: number;
  /** Null when only one zone is taught, so there is no runner-up to be ahead of. */
  margin?: number | null;
  scores?: { zone_id: string; distance: number }[];
  reason?: string;
}

export interface Metrics {
  type: "metrics";
  t: number;
  node_id: number;
  n_sub: number;
  rate_hz?: number;
  presence?: PresenceState;
  breathing?: VitalsState;
  heart?: VitalsState;
  /**
   * Why there is no estimate, when the reason is worth showing. Set when the analysis window
   * had a hole in it big enough that interpolating across it would have invented a peak in the
   * band being measured — a shared access point going quiet, not a fault in the node.
   */
  breathing_rejected?: string;
  heart_rejected?: string;
  placement?: { breathing_snr_db: number; heart_snr_db: number };
  selection?: SelectionState;
  variance?: { subcarriers: number[]; values: number[]; agc_fraction: number };
  zone?: ZoneState;
}

export interface Zone {
  id: string;
  name: string;
  created_at: number;
  notes: string;
}

export interface ZoneSample {
  id: string;
  zone_id: string;
  node_id: number;
  recorded_at: number;
  duration_s: number;
  frames: number;
  rate_hz: number;
  n_sub: number;
  mask_key: string;
  src_mac: string;
  channel: number;
  /** Median motion index over the capture, and the presence enter threshold it was measured
   *  against. `quiet` is derived from the two server-side, and is only as meaningful as the
   *  detector's calibration was. `motion_enter` of 0 means it was never measured, and then
   *  `quiet` is always false. */
  motion_median: number;
  motion_enter: number;
  quiet: boolean;
  bytes: number;
  /** Leave-one-out verdict. `only-sample` means its zone had no other example to score against. */
  verdict: "correct" | "wrong" | "only-sample" | "unusable" | "unscored";
  predicted: string | null;
}

export interface ZoneAccuracy {
  samples: number;
  scored: number;
  correct: number;
  accuracy: number | null;
}

/** A capture in flight: a countdown to walk there, then the window taken off the ring. */
export interface ZoneCapture {
  zone_id: string;
  zone_name: string;
  node_id: number;
  duration_s: number;
  countdown_s: number;
  phase: "countdown" | "recording" | "saving";
  remaining_s: number;
  started_at: number;
  link_epoch: number;
}

export interface ZoneCaptureResult {
  ok: boolean;
  error?: string;
  sample?: ZoneSample;
}

export interface ZoneReport {
  zones: Zone[];
  samples: ZoneSample[];
  accuracy: Record<string, ZoneAccuracy>;
  confusion: Record<string, Record<string, number>>;
  /** Zone pairs mistaken for each other in *both* directions — more examples will not fix it. */
  indistinguishable: { zones: string[]; count: number }[];
  bytes: number;
  max_bytes: number;
  feature_version: number;
  capture: ZoneCapture | null;
}

export interface Session {
  id: string;
  label: string;
  path: string;
  started_at: number;
  ended_at: number | null;
  frames: number;
  bytes: number;
  node_ids: number[];
  first_t_us: number | null;
  last_t_us: number | null;
  n_sub: number;
  notes: string;
  duration_s: number;
  active: boolean;
}

export interface ReplayState {
  path: string;
  playing: boolean;
  finished: boolean;
  speed: number;
  loop: boolean;
  position_us: number;
  first_t_us: number | null;
  last_t_us: number | null;
  frames_sent: number;
}

export interface ServerConfig {
  preprocess: {
    norm_mode: "hybrid" | "rssi" | "rms" | "none";
    drop_pilots: boolean;
    drop_dc_adjacent: boolean;
    hampel_enabled: boolean;
    agc_step_db: number;
    agc_uniformity: number;
  };
  presence: {
    window_s: number;
    calibration_s: number;
    enter_sigma: number;
    exit_sigma: number;
    debounce_s: number;
    use_pca: boolean;
    gate_quantile: number;
    top_k: number;
  };
  breathing: {
    window_s: number;
    band: [number, number];
    n_subcarriers: number;
    gate_quantile: number;
  };
  heart: {
    window_s: number;
    band: [number, number];
    n_subcarriers: number;
    gate_quantile: number;
  };
  zones: {
    window_s: number;
    sample_s: number;
    band: [number, number];
    vote_s: number;
    reject_sigma: number;
    reject_separation_frac: number;
    margin_frac: number;
  };
}

/** An access point seen in a node's last WiFi scan. */
export interface ScanAp {
  bssid: string;
  ssid: string;
  freq: number;
  channel: number;
  width: number;
  signal: number | null;
  stations: number | null;
  utilisation: number | null;
  associated: boolean;
}

/** A transmitter a node has heard while capturing, from the frames themselves. */
export interface Transmitter {
  mac: string;
  frames: number;
  rssi: number;
  channel: number;
  last_seen: number;
}

/**
 * One transmitter's contribution to a node's capture, over the traffic window.
 *
 * `counts` is frames per second, oldest first, and every source's array is aligned to the same
 * seconds as its node's — which is what lets them be stacked without any interpolation.
 */
export interface TrafficSource {
  mac: string;
  /** Since the node appeared. `frames_window` is the same count over the last `window_s`. */
  frames: number;
  frames_window: number;
  rate_hz: number;
  /** Fraction of the node's frames in the window that came from this transmitter. */
  share: number;
  counts: number[];
  rssi: number;
  /** Null when this source sent nothing inside the window. */
  rssi_mean: number | null;
  rssi_min: number | null;
  rssi_max: number | null;
  channel: number;
  n_sub: number;
  max_gap_s: number;
  duty: number;
  first_seen: number;
  last_seen: number;
  /** True only when the node's own scan saw this BSSID; `ssid` names it. */
  ap: boolean;
  ssid: string;
  /** Locally administered address — what MAC randomization looks like from the air. */
  randomized: boolean;
  /** A group bit in a source address, which cannot happen on the air. See traffic.py. */
  malformed: boolean;
}

/** The last `window_s` seconds of one node's capture, broken down by transmitter. */
export interface TrafficNode {
  node_id: number;
  online: boolean;
  window_s: number;
  /** How much of the window the node has been around for; the rate is divided by this. */
  covered_s: number;
  counts: number[];
  frames: number;
  frames_window: number;
  rate_hz: number;
  /** Fraction of covered seconds that carried at least one frame. */
  duty: number;
  silent_s: number;
  max_gap_s: number;
  /** Frames with no source address: a v1 uplink, which has no such field. */
  anonymous: number;
  evicted: number;
  first_seen: number;
  last_seen: number;
  sources: TrafficSource[];
}

/**
 * One node's control picture: what the operator asked for (`desired`), what the node last said
 * it applied (`applied`), and whether those agree yet. A node that never reports leaves
 * `applied` null — the UI reads that as "this node does not take control" and shows it read-only.
 */
export interface NodeControl {
  node_id: number;
  desired: { channel: string; stimulus: "auto" | "always" | "off" };
  revision: number;
  scan_rev: number;
  applied: {
    channel?: string;
    stimulus?: string;
    narrowband?: boolean;
    observed_channel?: number;
    capabilities?: string[];
  } | null;
  reported_rev: number | null;
  reported_ts: number | null;
  scan: { ts: number; aps: ScanAp[] } | null;
  reported_scan_rev: number | null;
}

export interface WifiNode extends NodeControl {
  transmitters: Transmitter[];
}

export interface Layout {
  n_sub: number;
  name: string;
  n_valid: number;
  valid_indices: number[];
  k: number[];
}

/** Which build the server is running. `commit` and `built_at` are empty for a dev build. */
export interface BuildInfo {
  version: string;
  commit: string;
  built_at: string;
}

export interface Snapshot {
  uptime_s: number;
  build: BuildInfo;
  nodes: NodeHealth[];
  layout: Layout | null;
  recording: Session | null;
  replay: ReplayState | null;
  zone_capture: ZoneCapture | null;
  counters: {
    live_frames: number;
    replay_frames: number;
    bad_packets: number;
    suppressed_live: number;
    clients: number;
  };
  config: ServerConfig;
}

export type ServerEvent =
  | ({ type: "hello" } & Snapshot)
  | Metrics
  | { type: "nodes"; nodes: NodeHealth[] }
  | { type: "recording"; session: Session | null }
  | { type: "replay"; replay: ReplayState | null; session_id?: string }
  | { type: "config"; config: ServerConfig }
  | { type: "node_control"; control: NodeControl }
  | { type: "zones" }
  | { type: "zone_capture"; capture: ZoneCapture | null; result?: ZoneCaptureResult }
  | { type: "sessions_pruned"; session_ids: string[] }
  | { type: "recalibrated"; node_id: number | null }
  | { type: "pong"; t: number };

/** A decoded batch of CSI frames, posted from the worker to the main thread. */
export interface FrameBatch {
  kind: "frames";
  nodeId: number;
  nSub: number;
  /** `count * nSub` amplitudes, frame-major. Transferred, not copied. */
  amp: Float32Array;
  count: number;
  /** Per-frame metadata, all length `count`. */
  timestamps: Float64Array;
  rssi: Int8Array;
  agc: Uint8Array;
  replay: boolean;
}

export type WorkerOut =
  | FrameBatch
  | { kind: "event"; event: ServerEvent }
  | { kind: "status"; connected: boolean; attempt: number }
  | { kind: "stats"; framesPerSecond: number; dropped: number };

export type WorkerIn =
  | { kind: "connect"; url: string }
  | { kind: "send"; message: unknown }
  | { kind: "select"; nodeId: number | null };
