// Downlink binary format (server -> browser). Mirrored in server/csi/downlink.py; see
// docs/wire-format.md.

export const DOWN_MAGIC = 0x4344;
export const DOWN_VERSION = 1;
export const DOWN_HEADER_SIZE = 24;

export const FLAG_AGC_STEP = 1 << 0;
export const FLAG_REPLAY = 1 << 1;

export const HIST_MAGIC = 0x4348;
export const HIST_VERSION = 1;
export const HIST_HEADER_SIZE = 16;

export interface CsiHistory {
  nodeId: number;
  nSub: number;
  count: number;
  /** Device microseconds, oldest first. */
  timestamps: Float64Array;
  /** Amplitude, row-major `count x nSub`. NaN where the subcarrier is masked. */
  amp: Float32Array;
  /** 1 where the receiver changed gain on that column. */
  agc: Uint8Array;
}

/**
 * Parse the block from `GET /api/nodes/{id}/history` — the server's ring, so a client that has
 * just connected can draw the last couple of minutes instead of starting from an empty canvas.
 *
 * The header is 16 bytes rather than the 12 its fields need so the timestamps land 8-byte
 * aligned; `new Float64Array` on an unaligned offset throws, and the amplitudes after them are
 * then 4-aligned for free.
 */
export function decodeHistory(buffer: ArrayBuffer): CsiHistory | null {
  if (buffer.byteLength < HIST_HEADER_SIZE) return null;

  const view = new DataView(buffer);
  if (view.getUint16(0, true) !== HIST_MAGIC) return null;
  if (view.getUint8(2) !== HIST_VERSION) return null;

  const nodeId = view.getUint8(3);
  const nSub = view.getUint16(4, true);
  const count = view.getUint32(8, true);
  if (buffer.byteLength !== HIST_HEADER_SIZE + count * (8 + 4 * nSub + 1)) return null;

  const ampAt = HIST_HEADER_SIZE + 8 * count;
  return {
    nodeId,
    nSub,
    count,
    timestamps: new Float64Array(buffer, HIST_HEADER_SIZE, count),
    amp: new Float32Array(buffer, ampAt, count * nSub),
    agc: new Uint8Array(buffer, ampAt + 4 * count * nSub, count),
  };
}

export interface CsiFrame {
  nodeId: number;
  seq: number;
  /** Device microseconds since boot. */
  timestamp: number;
  rssi: number;
  noiseFloor: number;
  channel: number;
  flags: number;
  agcStep: boolean;
  replay: boolean;
  nSub: number;
  /** Amplitude per subcarrier. NaN where the subcarrier is masked out (guard, DC, pilot). */
  amp: Float32Array;
}

/**
 * Parse one binary WebSocket message. Returns null for anything that is not a CSI frame, so a
 * future message type does not have to be a breaking change.
 *
 * The header is 24 bytes specifically so `amp` lands 4-byte aligned and this can be a view
 * rather than a copy — `new Float32Array` on an unaligned offset throws.
 */
export function decodeFrame(buffer: ArrayBuffer): CsiFrame | null {
  if (buffer.byteLength < DOWN_HEADER_SIZE) return null;

  const view = new DataView(buffer);
  if (view.getUint16(0, true) !== DOWN_MAGIC) return null;
  if (view.getUint8(2) !== DOWN_VERSION) return null;

  const nSub = view.getUint16(20, true);
  if (buffer.byteLength !== DOWN_HEADER_SIZE + 4 * nSub) return null;

  const flags = view.getUint8(19);
  return {
    nodeId: view.getUint8(3),
    seq: view.getUint32(4, true),
    // Microseconds since boot stays exact in a double for ~285 years, so the BigInt can go.
    timestamp: Number(view.getBigUint64(8, true)),
    rssi: view.getInt8(16),
    noiseFloor: view.getInt8(17),
    channel: view.getUint8(18),
    flags,
    agcStep: (flags & FLAG_AGC_STEP) !== 0,
    replay: (flags & FLAG_REPLAY) !== 0,
    nSub,
    amp: new Float32Array(buffer, DOWN_HEADER_SIZE, nSub),
  };
}
