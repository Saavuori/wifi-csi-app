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
  snr_db: number;
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
  confidence: number;
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

export interface Snapshot {
  uptime_s: number;
  nodes: NodeHealth[];
  layout: Layout | null;
  recording: Session | null;
  replay: ReplayState | null;
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
