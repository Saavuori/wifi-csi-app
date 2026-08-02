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

/*
 * Access points. These cross HTTP rather than the socket, because the server does not own this
 * state: nmcli and the nexmon tooling live on the host, outside the container, and an agent
 * there publishes the list and carries out selections through the shared data directory. The
 * server relays; these are the shapes it relays.
 */

/** One radio the host can currently hear, as `nmcli dev wifi list` reports it. */
export interface AccessPoint {
  bssid: string;
  ssid: string;
  channel: number;
  /** nmcli's SIGNAL: a 0–100 quality percentage, not dBm. Higher is stronger. */
  signal: number;
  /** True for the radio the probe dongle is associated to. At most one AP has it. */
  in_use: boolean;
}

/**
 * How traffic is provoked out of the measured radio.
 *
 * The extractor only produces CSI for HT/VHT frames, so what is being bought is a *unicast*
 * transmission from that access point — to us, as an acknowledgement, or to one of its clients.
 * Broadcasting at it yields nothing, because a group-addressed frame leaves at a legacy rate
 * with no HT channel estimate to read out.
 */
export type ProbeMode = "broadcast" | "unicast" | "icmp";

/**
 * What the probe is doing, and — when the node has audited itself — what it is buying.
 *
 * Every field past `host`/`hz` is optional because an agent older than this UI publishes only
 * those two, and an unaudited node publishes no rates at all. Absent means "not reported", which
 * the UI has to say rather than dress up as a zero.
 */
export interface ApProbe {
  /** Empty in broadcast mode, where the target is the interface's own broadcast address. */
  host: string;
  hz: number;
  mode?: ProbeMode;
  /** Capture rate attributable to the probes: total minus background. The number that matters. */
  induced_hz?: number;
  /** Frames per second arriving overall while probing. */
  total_hz?: number;
  /** What arrived with the probes off — the household's own traffic to that access point. */
  background_hz?: number;
}

/** What the host agent publishes about the radios, and about which one is being measured. */
export interface ApStatus {
  /**
   * Always sent. False when the host agent has published nothing at all — no agent running, or
   * a data directory that is not actually shared — in which case the rest of this is empty.
   */
  available?: boolean;
  updated_at?: number;
  /** "auto" follows whatever the dongle associated to; "manual" holds the chosen BSSID. */
  follow?: "auto" | "manual";
  /**
   * What carries the node's own traffic, which decides whether probing can work at all: a
   * "dongle" is associated to some access point and can provoke its acknowledgements, while
   * "wired" has no radio of its own and can only reach an access point through its clients.
   * Absent from an agent too old to publish it.
   */
  uplink?: "dongle" | "wired";
  /** Empty strings while the capture has not settled on anything yet. */
  measured?: { bssid: string; chanspec: string };
  probe?: ApProbe;
  aps: AccessPoint[];
}

/**
 * A request to measure a different radio, and optionally to change how traffic is provoked.
 * `bssid` is ignored when the mode is "auto"; every probe field is optional, and omitting one
 * leaves the host's current setting alone.
 */
export interface ApSelectRequest {
  mode: "auto" | "manual";
  bssid?: string;
  probe_host?: string;
  probe_mode?: ProbeMode;
  probe_hz?: number;
}

/** The server's acknowledgement: the id to poll for the host agent's answer. */
export interface ApSelectAccepted {
  id: string;
}

/** A poll of a pending selection: still waiting, or the host agent's verdict. */
export type ApSelectStatus =
  | { pending: true }
  | { pending?: false; id: string; ok: boolean; error?: string; finished_at?: number };

/** What a completed selection is worth telling the user, whichever way it went. */
export interface ApSelectOutcome {
  ok: boolean;
  error: string;
}
