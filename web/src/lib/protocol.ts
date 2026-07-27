// Downlink binary format (server -> browser). Mirrored in server/csi/downlink.py; see
// docs/wire-format.md.

export const DOWN_MAGIC = 0x4344;
export const DOWN_VERSION = 1;
export const DOWN_HEADER_SIZE = 24;

export const FLAG_AGC_STEP = 1 << 0;
export const FLAG_REPLAY = 1 << 1;

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
