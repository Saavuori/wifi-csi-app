// Minimal canvas plotting shared by the line views.
//
// Not a chart library: everything here draws into a caller-owned context with a caller-owned
// scale, because the views need to overlay things (thresholds, hysteresis bands, selected
// subcarriers) that a general chart abstraction would fight.

export interface Plot {
  context: CanvasRenderingContext2D;
  width: number;
  height: number;
  ratio: number;
  padLeft: number;
  padBottom: number;
  padTop: number;
  padRight: number;
  fontPx: number;
}

export function beginPlot(canvas: HTMLCanvasElement, options: Partial<Plot> = {}): Plot | null {
  const context = canvas.getContext("2d");
  if (context === null) return null;

  const ratio = window.devicePixelRatio || 1;
  // Gutters sized from the canvas rather than fixed. A 44 px axis gutter is a rounding error on
  // a 900 px-wide plot and an eighth of a phone's, and the axis labels are the same handful of
  // short numbers either way — so on a narrow canvas they get a narrower margin and a smaller
  // face, and the data keeps the width it should have had all along.
  const narrow = canvas.clientWidth > 0 && canvas.clientWidth < 480;
  const plot: Plot = {
    context,
    width: canvas.width,
    height: canvas.height,
    ratio,
    padLeft: (narrow ? 30 : 44) * ratio,
    padBottom: (narrow ? 18 : 24) * ratio,
    padTop: (narrow ? 8 : 10) * ratio,
    padRight: (narrow ? 8 : 10) * ratio,
    fontPx: narrow ? 9 : 10,
    ...options,
  };

  context.clearRect(0, 0, plot.width, plot.height);
  return plot;
}

export function plotArea(plot: Plot) {
  return {
    x0: plot.padLeft,
    y0: plot.padTop,
    x1: plot.width - plot.padRight,
    y1: plot.height - plot.padBottom,
    get w() {
      return this.x1 - this.x0;
    },
    get h() {
      return this.y1 - this.y0;
    },
  };
}

export function drawFrame(plot: Plot, xLabels: [number, string][], yLabels: [number, string][]) {
  const area = plotArea(plot);
  const { context, ratio } = plot;

  context.strokeStyle = "rgba(255,255,255,0.10)";
  context.fillStyle = "rgba(255,255,255,0.45)";
  context.font = `${plot.fontPx * ratio}px ui-monospace, monospace`;
  context.lineWidth = ratio;

  context.textAlign = "right";
  context.textBaseline = "middle";
  for (const [position, label] of yLabels) {
    const y = area.y1 - position * area.h;
    context.beginPath();
    context.moveTo(area.x0, y);
    context.lineTo(area.x1, y);
    context.stroke();
    context.fillText(label, area.x0 - 4 * ratio, y);
  }

  context.textBaseline = "top";
  for (const [position, label] of xLabels) {
    // Labels at the very ends are anchored inwards instead of centred. Centred, half of "older"
    // hangs off the left edge of the canvas — invisible on a desktop's wide plot, and half the
    // label on a phone's.
    context.textAlign = position <= 0.01 ? "left" : position >= 0.99 ? "right" : "center";
    const x = area.x0 + position * area.w;
    context.fillText(label, x, area.y1 + 5 * ratio);
  }
}

/** Draw a series given in normalized [0,1] coordinates, y up. */
export function drawSeries(
  plot: Plot,
  points: ArrayLike<number>,
  options: { color: string; width?: number; fill?: string; xOf?: (i: number) => number },
) {
  const area = plotArea(plot);
  const { context, ratio } = plot;
  const n = points.length;
  if (n === 0) return;

  const xOf = options.xOf ?? ((i: number) => (n === 1 ? 0 : i / (n - 1)));

  context.beginPath();
  let started = false;
  for (let i = 0; i < n; i++) {
    const value = points[i];
    if (!Number.isFinite(value)) {
      started = false;
      continue;
    }
    const x = area.x0 + xOf(i) * area.w;
    const y = area.y1 - Math.max(0, Math.min(1, value)) * area.h;
    if (started) context.lineTo(x, y);
    else {
      context.moveTo(x, y);
      started = true;
    }
  }

  if (options.fill !== undefined) {
    context.lineTo(area.x1, area.y1);
    context.lineTo(area.x0, area.y1);
    context.closePath();
    context.fillStyle = options.fill;
    context.fill();
  }

  context.strokeStyle = options.color;
  context.lineWidth = (options.width ?? 1.4) * ratio;
  context.stroke();
}

export function drawHorizontal(plot: Plot, value: number, color: string, dashed = true) {
  const area = plotArea(plot);
  const { context, ratio } = plot;
  const y = area.y1 - Math.max(0, Math.min(1, value)) * area.h;

  context.save();
  if (dashed) context.setLineDash([4 * ratio, 4 * ratio]);
  context.strokeStyle = color;
  context.lineWidth = ratio;
  context.beginPath();
  context.moveTo(area.x0, y);
  context.lineTo(area.x1, y);
  context.stroke();
  context.restore();
}

/** Shade a horizontal band, used for the presence hysteresis gap and the analysis passband. */
export function drawBand(plot: Plot, low: number, high: number, color: string) {
  const area = plotArea(plot);
  const yLow = area.y1 - Math.max(0, Math.min(1, low)) * area.h;
  const yHigh = area.y1 - Math.max(0, Math.min(1, high)) * area.h;
  plot.context.fillStyle = color;
  plot.context.fillRect(area.x0, yHigh, area.w, yLow - yHigh);
}

/** A palette for overlaid series, chosen to stay distinguishable on a dark background. */
export const SERIES_COLORS = [
  "#4cc9f0",
  "#ffd166",
  "#06d6a0",
  "#ef476f",
  "#b388eb",
  "#f78c6b",
  "#8de969",
  "#9aa5b1",
];
