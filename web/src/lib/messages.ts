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
