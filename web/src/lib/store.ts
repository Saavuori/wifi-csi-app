// Application state and the single connection to the server.
//
// Views subscribe to what they need. Frame batches are pushed to subscribers directly rather
// than being buffered here, because the only consumers that want them (the waterfall and the
// subcarrier lines) keep their own history in a shape suited to how they draw.

import type {
  BuildInfo,
  FrameBatch,
  Metrics,
  NodeHealth,
  ReplayState,
  ServerConfig,
  ServerEvent,
  Session,
  Snapshot,
  TrafficNode,
  WifiNode,
  WorkerIn,
  WorkerOut,
  ZoneCapture,
  ZoneCaptureResult,
  ZoneReport,
} from "./messages";

type Listener<T> = (value: T) => void;

class Signal<T> {
  private listeners = new Set<Listener<T>>();

  constructor(public value: T) {}

  subscribe(listener: Listener<T>, immediate = true): () => void {
    this.listeners.add(listener);
    if (immediate) listener(this.value);
    return () => this.listeners.delete(listener);
  }

  set(value: T) {
    this.value = value;
    for (const listener of this.listeners) listener(value);
  }
}

export class Store {
  readonly connected = new Signal(false);
  readonly nodes = new Signal<NodeHealth[]>([]);
  readonly selectedNode = new Signal<number | null>(null);
  readonly metrics = new Signal<Map<number, Metrics>>(new Map());
  readonly config = new Signal<ServerConfig | null>(null);
  readonly recording = new Signal<Session | null>(null);
  readonly replay = new Signal<ReplayState | null>(null);
  readonly sessions = new Signal<Session[]>([]);
  readonly wifi = new Signal<WifiNode[]>([]);
  readonly traffic = new Signal<TrafficNode[]>([]);
  readonly zones = new Signal<ZoneReport | null>(null);
  readonly zoneCapture = new Signal<ZoneCapture | null>(null);
  /** The outcome of the last capture, so a refusal can be shown rather than looking like a
   *  button that did nothing. Cleared when the next capture starts. */
  readonly zoneResult = new Signal<ZoneCaptureResult | null>(null);
  readonly rate = new Signal(0);
  readonly build = new Signal<BuildInfo | null>(null);

  private worker: Worker;
  private frameListeners = new Set<Listener<FrameBatch>>();

  constructor() {
    this.worker = new Worker(new URL("../workers/socket.ts", import.meta.url), {
      type: "module",
    });
    this.worker.onmessage = (event: MessageEvent<WorkerOut>) => this.onWorkerMessage(event.data);

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    this.send({ kind: "connect", url: `${protocol}//${location.host}/ws` });
  }

  onFrames(listener: Listener<FrameBatch>): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  selectNode(nodeId: number | null) {
    if (this.selectedNode.value === nodeId) return;
    this.selectedNode.set(nodeId);
    // Filtering in the worker means an unselected node's frames are dropped before they are
    // decoded, not after.
    this.send({ kind: "select", nodeId });
  }

  /** Current metrics for the selected node, if any. */
  currentMetrics(): Metrics | null {
    const id = this.selectedNode.value;
    if (id === null) return null;
    return this.metrics.value.get(id) ?? null;
  }

  patchConfig(patch: Record<string, unknown>) {
    this.send({ kind: "send", message: { type: "config", config: patch } });
  }

  recalibrate(nodeId: number | null) {
    this.send({ kind: "send", message: { type: "recalibrate", node_id: nodeId } });
  }

  /** Fetch the WiFi overview: control state, last scan and heard transmitters, per node. */
  async refreshWifi() {
    try {
      const response = await fetch("/api/wifi");
      const body = (await response.json()) as { nodes: WifiNode[] };
      this.wifi.set(body.nodes);
    } catch {
      // Offline. The caller polls, and the next tick will pick it up.
    }
  }

  /**
   * Fetch the traffic breakdown: per node, the last two minutes of capture by transmitter.
   *
   * Polled rather than pushed. The server buckets by the second, so nothing here can move
   * faster than 1 Hz, and a table of transmitters riding the 5 Hz metrics broadcast would cost
   * every connected client more bytes than the analysis it exists to deliver.
   */
  async refreshTraffic() {
    try {
      const response = await fetch("/api/traffic");
      const body = (await response.json()) as { nodes: TrafficNode[] };
      this.traffic.set(body.nodes);
    } catch {
      // Offline. The caller polls, and the next tick will pick it up.
    }
  }

  /** Change a node's desired channel and/or stimulus mode. Returns the server's echo. */
  async patchNodeControl(
    nodeId: number,
    patch: { channel?: string; stimulus?: "auto" | "always" | "off" },
  ) {
    const response = await fetch(`/api/nodes/${nodeId}/control`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!response.ok) throw new Error(`control patch failed: ${response.status}`);
    await this.refreshWifi();
  }

  /** Ask a node to run a WiFi scan on its next poll. */
  async requestScan(nodeId: number) {
    await fetch(`/api/nodes/${nodeId}/control/scan`, { method: "POST" });
    await this.refreshWifi();
  }

  /** Zone definitions, their examples, and how well they separate. */
  async refreshZones() {
    try {
      const response = await fetch("/api/zones");
      const body = (await response.json()) as ZoneReport;
      this.zones.set(body);
      this.zoneCapture.set(body.capture);
    } catch {
      // Offline. The WebSocket reconnect triggers another refresh.
    }
  }

  /**
   * Start recording an example. The server counts down, then takes the window off the ring.
   *
   * Returns the server's refusal text rather than throwing, because every reason this can fail
   * is one the person standing in the kitchen needs to read: no frames from that node, a
   * capture already running, or the examples directory at its budget.
   */
  async recordZoneSample(
    zoneId: string,
    nodeId: number,
    options: { duration_s: number; countdown_s: number },
  ): Promise<string | null> {
    this.zoneResult.set(null);
    const response = await fetch(`/api/zones/${zoneId}/samples`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ node_id: nodeId, ...options }),
    });
    if (response.ok) return null;
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    return body.detail ?? `could not start recording (${response.status})`;
  }

  async cancelZoneCapture() {
    await fetch("/api/zones/capture/cancel", { method: "POST" });
  }

  async createZone(name: string) {
    await fetch("/api/zones", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    });
    await this.refreshZones();
  }

  async renameZone(zoneId: string, name: string) {
    await fetch(`/api/zones/${zoneId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    });
    await this.refreshZones();
  }

  async deleteZone(zoneId: string) {
    await fetch(`/api/zones/${zoneId}`, { method: "DELETE" });
    await this.refreshZones();
  }

  async deleteZoneSample(zoneId: string, sampleId: string) {
    await fetch(`/api/zones/${zoneId}/samples/${sampleId}`, { method: "DELETE" });
    await this.refreshZones();
  }

  private send(message: WorkerIn) {
    this.worker.postMessage(message);
  }

  private onWorkerMessage(message: WorkerOut) {
    switch (message.kind) {
      case "status":
        this.connected.set(message.connected);
        break;
      case "frames":
        for (const listener of this.frameListeners) listener(message);
        break;
      case "stats":
        this.rate.set(message.framesPerSecond);
        break;
      case "event":
        this.onEvent(message.event);
        break;
    }
  }

  private onEvent(event: ServerEvent) {
    switch (event.type) {
      case "hello":
        this.applySnapshot(event);
        break;
      case "nodes":
        this.setNodes(event.nodes);
        break;
      case "metrics": {
        const next = new Map(this.metrics.value);
        next.set(event.node_id, event);
        this.metrics.set(next);
        break;
      }
      case "recording":
        this.recording.set(event.session);
        void this.refreshSessions();
        break;
      case "replay":
        this.replay.set(event.replay);
        break;
      case "config":
        this.config.set(event.config);
        break;
      case "sessions_pruned":
        // Retention deleted the oldest recordings. Without this the sessions list keeps
        // offering files that are gone, and replaying one 404s with no explanation.
        void this.refreshSessions();
        break;
      case "node_control":
        // A control change (from this browser or another) landed. Re-pull the overview so the
        // desired/applied/pending picture and the scan results stay current without a poll.
        void this.refreshWifi();
        break;
      case "zones":
        void this.refreshZones();
        break;
      case "zone_capture":
        this.zoneCapture.set(event.capture);
        // The result only arrives on the broadcast that ends a capture. Keeping the previous
        // one would leave a stale "saved" or a stale error on screen through the next attempt.
        if (event.result !== undefined) this.zoneResult.set(event.result);
        else if (event.capture !== null) this.zoneResult.set(null);
        break;
      default:
        break;
    }
  }

  private applySnapshot(snapshot: Snapshot) {
    this.setNodes(snapshot.nodes);
    if (snapshot.build) this.build.set(snapshot.build);
    this.config.set(snapshot.config);
    this.recording.set(snapshot.recording);
    this.replay.set(snapshot.replay);
    this.zoneCapture.set(snapshot.zone_capture ?? null);
    void this.refreshSessions();
    void this.refreshZones();
  }

  private setNodes(nodes: NodeHealth[]) {
    this.nodes.set(nodes);
    // Land on something as soon as a node appears, so the app is never showing an empty view
    // while data is arriving. Prefer an online node over a stale one.
    if (this.selectedNode.value === null && nodes.length > 0) {
      const online = nodes.find((node) => node.online) ?? nodes[0];
      this.selectNode(online.node_id);
    }
  }

  async refreshSessions() {
    try {
      const response = await fetch("/api/sessions");
      const body = (await response.json()) as { sessions: Session[] };
      this.sessions.set(body.sessions);
    } catch {
      // Offline. The WebSocket reconnect will trigger another refresh.
    }
  }
}

export const store = new Store();
