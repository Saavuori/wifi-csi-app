// Application state and the single connection to the server.
//
// Views subscribe to what they need. Frame batches are pushed to subscribers directly rather
// than being buffered here, because the only consumers that want them (the waterfall and the
// subcarrier lines) keep their own history in a shape suited to how they draw.

import type {
  ApSelectAccepted,
  ApSelectOutcome,
  ApSelectRequest,
  ApSelectStatus,
  ApStatus,
  FrameBatch,
  Metrics,
  NodeHealth,
  ReplayState,
  ServerConfig,
  ServerEvent,
  Session,
  Snapshot,
  WorkerIn,
  WorkerOut,
} from "./messages";

// How a selection is waited on. Applying one restarts the capture on the host, so the answer is
// several seconds away at best; the timeout is long enough that a slow restart is not reported
// as a failure, and short enough that a dead agent does not leave the UI spinning forever.
const AP_SELECT_POLL_MS = 750;
const AP_SELECT_TIMEOUT_MS = 60_000;

type Listener<T> = (value: T) => void;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

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
  readonly rate = new Signal(0);
  readonly aps = new Signal<ApStatus | null>(null);

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
      default:
        break;
    }
  }

  private applySnapshot(snapshot: Snapshot) {
    this.setNodes(snapshot.nodes);
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

  async refreshAps() {
    try {
      const response = await fetch("/api/aps");
      const body = (await response.json()) as ApStatus;
      // A server too old to know this route answers with the SPA's index page rather than a
      // 404, so "did it parse" is not enough of a test. Anything without a list of radios in it
      // is the same situation as no host agent, and says so.
      this.aps.set(Array.isArray(body?.aps) ? body : { available: false, aps: [] });
    } catch {
      // Unreachable. Keep the last list on screen rather than blanking it: it is still the
      // truth about the room as of a moment ago, and the next poll will correct it.
    }
  }

  /**
   * Ask the host to measure a different radio, and wait for it to say whether it managed.
   *
   * Two hops, because the server cannot do this itself: it runs in a container and nmcli, the
   * unit files and the nexmon tooling are all on the host. So the server writes a request into
   * the shared data directory, an agent on the host acts on it, and we poll for the result.
   *
   * This cannot strand the machine, and that is worth being explicit about. What is being chosen
   * is the radio the *monitor* interface listens to, plus how that radio is provoked into
   * transmitting. Whatever carries the route into the Pi — a USB dongle's association, or eth0
   * on a wired node — is a different interface entirely and is never touched, so a bad choice
   * costs a useless measurement and nothing else.
   */
  async selectAp(request: ApSelectRequest): Promise<ApSelectOutcome> {
    let id: string;
    try {
      const response = await fetch("/api/aps/select", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(request),
      });
      const body = (await response.json()) as Partial<ApSelectAccepted> & {
        error?: string;
        detail?: string;
      };
      if (!response.ok || typeof body.id !== "string") {
        return {
          ok: false,
          error: body.error ?? body.detail ?? `the server refused the request (${response.status})`,
        };
      }
      id = body.id;
    } catch {
      return { ok: false, error: "could not reach the server" };
    }

    const deadline = Date.now() + AP_SELECT_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await sleep(AP_SELECT_POLL_MS);
      try {
        const response = await fetch(`/api/aps/select/${encodeURIComponent(id)}`);
        const status = (await response.json()) as ApSelectStatus;
        if (status.pending) continue;
        // Whatever happened, what is being measured may have moved. Ask.
        void this.refreshAps();
        return { ok: status.ok, error: status.error ?? "" };
      } catch {
        // A failed poll is not a failed selection — the host agent works from a file and does
        // not know or care that a browser lost the server. Keep asking until the deadline.
      }
    }
    return {
      ok: false,
      error: "the host did not answer in time; check that the AP agent is running on the Pi",
    };
  }
}

export const store = new Store();
