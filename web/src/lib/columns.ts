// The waterfall's own history: a ring of CSI columns held on the client.
//
// The canvas used to be the only place history existed. That works while the app is open and
// nothing resizes, and fails at exactly the moments a user notices: opening the page draws an
// empty plot that takes a screen-width of frames to fill, and there is no way to look at
// something that has already scrolled off. Keeping the columns means the picture can be redrawn
// from data — which is what makes both backfill and rewind possible at all.
//
// Preallocated and written in place, for the same reason the server's ring is: at 100 Hz per
// node, allocating per column is how you get a GC pause in the middle of the view whose whole
// job is to look smooth.

/** Amplitudes are float32, timestamps float64, the AGC flag one byte. */
function bytesPerColumn(nSub: number): number {
  return nSub * 4 + 8 + 1;
}

// How far back the user can scrub. Two ceilings, because the two node families differ by 4x in
// how much a column costs: an ESP32 sends 64 subcarriers and a Pi 256. The time bound is what
// is wanted; the byte bound is what keeps a 256-subcarrier node from spending 60 MB on it.
const MAX_SECONDS = 600;
const NOMINAL_HZ = 100;
const MAX_BYTES = 32 * 1024 * 1024;
const MIN_COLUMNS = 2048;

export function capacityFor(nSub: number): number {
  const byTime = MAX_SECONDS * NOMINAL_HZ;
  const byBytes = Math.floor(MAX_BYTES / bytesPerColumn(nSub));
  return Math.max(MIN_COLUMNS, Math.min(byTime, byBytes));
}

/**
 * A fixed-capacity ring of columns, oldest-first when read.
 *
 * Indices handed out are *logical*: 0 is the oldest column still held, `count - 1` the newest.
 * They shift as the ring overwrites, which is the right behaviour for a scrubber anchored to a
 * position in the buffer rather than to a moment in time.
 */
export class ColumnStore {
  readonly nSub: number;
  readonly capacity: number;

  private readonly amp: Float32Array;
  private readonly time: Float64Array;
  private readonly agcFlags: Uint8Array;
  private write = 0;
  private held = 0;

  constructor(nSub: number, capacity = capacityFor(nSub)) {
    this.nSub = nSub;
    this.capacity = Math.max(1, capacity);
    this.amp = new Float32Array(this.capacity * nSub);
    this.time = new Float64Array(this.capacity);
    this.agcFlags = new Uint8Array(this.capacity);
  }

  get count(): number {
    return this.held;
  }

  get full(): boolean {
    return this.held === this.capacity;
  }

  push(amp: Float32Array, timestamp: number, agc: boolean): void {
    const at = this.write;
    this.amp.set(amp, at * this.nSub);
    this.time[at] = timestamp;
    this.agcFlags[at] = agc ? 1 : 0;
    this.write = (at + 1) % this.capacity;
    if (this.held < this.capacity) this.held += 1;
  }

  /** Physical slot backing logical index `i`. */
  private slot(i: number): number {
    return (this.write - this.held + i + this.capacity * 2) % this.capacity;
  }

  /**
   * Amplitudes for one column. A view onto the backing store, not a copy — valid until the ring
   * wraps far enough to overwrite it, which for a renderer reading it the same tick it asked is
   * never. Do not hold one across frames.
   */
  column(i: number): Float32Array {
    const at = this.slot(i) * this.nSub;
    return this.amp.subarray(at, at + this.nSub);
  }

  timestamp(i: number): number {
    return this.time[this.slot(i)];
  }

  agc(i: number): boolean {
    return this.agcFlags[this.slot(i)] === 1;
  }

  /** Seconds between two logical columns, by device clock. NaN if either index is out of range. */
  span(from: number, to: number): number {
    if (from < 0 || to < 0 || from >= this.held || to >= this.held) return NaN;
    return (this.timestamp(to) - this.timestamp(from)) / 1e6;
  }
}
