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

// How long a frame-rate window is. Short enough that the readout notices the stream stopping,
// long enough that a window holds tens of frames rather than a handful.
const STATS_MS = 500;

// Weight given to the newest window when updating the reported rate. At one window every 500 ms
// this settles within a couple of seconds, which is the balance a readout wants: quick enough to
// believe, steady enough to read.
const RATE_ALPHA = 0.3;

const pending = new Map<number, Pending>();
let selectedNode: number | null = null;
let framesThisWindow = 0;
let dropped = 0;
// Measured with performance.now() rather than Date.now(): it is monotonic, so a clock step in
// the middle of a window cannot produce a negative interval and a nonsense rate.
let windowStart = performance.now();
let smoothedHz = 0;

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

  framesThisWindow += 1;

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

  // A rate, not a count. `flush` runs on a timer that a busy tab is free to run late, so a
  // window is only ever *at least* STATS_MS long; dividing the frames in it by the nominal
  // length rather than the length that actually elapsed reports a rate that is wrong by however
  // late the timer was, which is most of why a steady node used to read as a number that
  // wandered. The rest of the wander is quantization — whole frames landing either side of a
  // boundary — and that is what the smoothing is for.
  const now = performance.now();
  const elapsed = now - windowStart;
  if (elapsed >= STATS_MS) {
    const instant = (framesThisWindow * 1000) / elapsed;
    // Smoothing is for quantization, not for hiding events. Easing symmetrically would do both:
    // a stream that stops still reads 29 fps four seconds later, because 0.7^n takes about
    // thirteen windows to fall from 100 to under 1, and a two-second stall reads as a slow sag
    // and a slow recovery. Neither is the node's behaviour, and a readout that keeps reporting
    // frames after the frames stopped is worse than one that twitches.
    //
    // So ease upward and toward small changes, but follow a real collapse immediately. The
    // threshold is what separates "a few frames landed the other side of a boundary" from
    // "something happened": half the running value is far outside the former.
    smoothedHz =
      smoothedHz < 1 || instant < smoothedHz / 2
        ? instant
        : smoothedHz + RATE_ALPHA * (instant - smoothedHz);
    post({ kind: "stats", framesPerSecond: Math.round(smoothedHz), dropped });
    framesThisWindow = 0;
    windowStart = now;
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
