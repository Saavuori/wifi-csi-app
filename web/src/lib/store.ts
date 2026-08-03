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
  WifiNode,
  WorkerIn,
  WorkerOut,
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
      case "node_control":
        // A control change (from this browser or another) landed. Re-pull the overview so the
        // desired/applied/pending picture and the scan results stay current without a poll.
        void this.refreshWifi();
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
    void this.refreshSessions();
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
