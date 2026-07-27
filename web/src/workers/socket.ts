/// <reference lib="webworker" />

// The WebSocket lives here, not on the main thread.
//
// Two things follow from that, and both are the point. Binary decode at 100 Hz per node never
// touches the thread that also has to render — so a busy waterfall cannot make the UI
// unresponsive, and a slow repaint cannot make the decoder fall behind. And frames arrive at
// the main thread already batched, which is what the renderer wants anyway: it draws on
// requestAnimationFrame at 60 Hz and blitting N columns per repaint is strictly cheaper than
// one draw per frame.
//
// Batches are posted with transferable buffers, so handing a batch across costs a pointer
// rather than a copy.

import { decodeFrame } from "../lib/protocol";
import type { ServerEvent, WorkerIn, WorkerOut } from "../lib/messages";

const scope = self as unknown as DedicatedWorkerGlobalScope;

// How often decoded frames are handed to the main thread. Slightly faster than a 60 Hz repaint,
// so the renderer always has something fresh without being woken more often than it can draw.
const FLUSH_MS = 12;

// If the main thread stops consuming (a backgrounded tab), stop growing the pending buffer.
// Dropping the oldest columns is the right failure: a waterfall that resumes at the current
// moment is useful, one that spends a minute catching up is not.
const MAX_PENDING_FRAMES = 600;

let socket: WebSocket | null = null;
let url = "";
let attempt = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

interface Pending {
  nodeId: number;
  nSub: number;
  amps: Float32Array[];
  timestamps: number[];
  rssi: number[];
  agc: number[];
  replay: boolean;
}

const pending = new Map<number, Pending>();
let selectedNode: number | null = null;
let framesThisSecond = 0;
let dropped = 0;
let lastStats = 0;

function post(message: WorkerOut, transfer: Transferable[] = []) {
  scope.postMessage(message, transfer);
}

function connect() {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";

  socket.onopen = () => {
    attempt = 0;
    post({ kind: "status", connected: true, attempt });
  };

  socket.onclose = () => {
    post({ kind: "status", connected: false, attempt });
    scheduleReconnect();
  };

  socket.onerror = () => {
    // onclose always follows, and that is where the reconnect lives. Handling it here too
    // would double the backoff.
  };

  socket.onmessage = (event: MessageEvent) => {
    if (typeof event.data === "string") {
      try {
        post({ kind: "event", event: JSON.parse(event.data) as ServerEvent });
      } catch {
        // A malformed event is not worth tearing down the socket for.
      }
      return;
    }
    ingest(event.data as ArrayBuffer);
  };
}

function scheduleReconnect() {
  attempt += 1;
  // Exponential backoff to 10 s. The node keeps sending regardless of whether a browser is
  // watching, and the server keeps recording, so there is nothing lost by backing off.
  const delay = Math.min(10_000, 250 * 2 ** Math.min(attempt, 6));
  reconnectTimer = setTimeout(connect, delay);
}

function ingest(buffer: ArrayBuffer) {
  const frame = decodeFrame(buffer);
  if (frame === null) return;
  if (selectedNode !== null && frame.nodeId !== selectedNode) return;

  framesThisSecond += 1;

  let batch = pending.get(frame.nodeId);
  if (batch === undefined || batch.nSub !== frame.nSub) {
    batch = {
      nodeId: frame.nodeId,
      nSub: frame.nSub,
      amps: [],
      timestamps: [],
      rssi: [],
      agc: [],
      replay: frame.replay,
    };
    pending.set(frame.nodeId, batch);
  }

  if (batch.amps.length >= MAX_PENDING_FRAMES) {
    batch.amps.shift();
    batch.timestamps.shift();
    batch.rssi.shift();
    batch.agc.shift();
    dropped += 1;
  }

  // `frame.amp` is a view onto `buffer`, which nothing else references once this returns.
  batch.amps.push(frame.amp);
  batch.timestamps.push(frame.timestamp);
  batch.rssi.push(frame.rssi);
  batch.agc.push(frame.agcStep ? 1 : 0);
  batch.replay = frame.replay;
}

function flush() {
  for (const [nodeId, batch] of pending) {
    const count = batch.amps.length;
    if (count === 0) continue;

    const amp = new Float32Array(count * batch.nSub);
    for (let i = 0; i < count; i++) {
      amp.set(batch.amps[i], i * batch.nSub);
    }

    post(
      {
        kind: "frames",
        nodeId,
        nSub: batch.nSub,
        amp,
        count,
        timestamps: Float64Array.from(batch.timestamps),
        rssi: Int8Array.from(batch.rssi),
        agc: Uint8Array.from(batch.agc),
        replay: batch.replay,
      },
      [amp.buffer],
    );

    batch.amps.length = 0;
    batch.timestamps.length = 0;
    batch.rssi.length = 0;
    batch.agc.length = 0;
  }

  const now = Date.now();
  if (now - lastStats >= 1000) {
    post({ kind: "stats", framesPerSecond: framesThisSecond, dropped });
    framesThisSecond = 0;
    lastStats = now;
  }
}

scope.onmessage = (event: MessageEvent<WorkerIn>) => {
  const message = event.data;
  switch (message.kind) {
    case "connect":
      url = message.url;
      connect();
      break;
    case "send":
      if (socket !== null && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(message.message));
      }
      break;
    case "select":
      selectedNode = message.nodeId;
      pending.clear();
      break;
  }
};

setInterval(flush, FLUSH_MS);
