// Colour maps for the waterfall.
//
// Perceptually uniform maps only. A rainbow map invents structure that is not in the data —
// its sharp lightness changes at cyan and yellow read as edges, and on a spectrogram that means
// seeing a boundary where there is only a gradient. That matters more here than usual, because
// the waterfall is the instrument you use to decide whether the capture chain works at all.

export type ColormapName = "viridis" | "inferno" | "gray";

// Anchor points sampled from matplotlib's maps, interpolated linearly into a 256-entry LUT.
// Eight anchors is enough: the maps are smooth by construction, and the residual error is well
// under one 8-bit step.
const ANCHORS: Record<ColormapName, [number, number, number][]> = {
  viridis: [
    [68, 1, 84],
    [72, 40, 120],
    [62, 74, 137],
    [49, 104, 142],
    [38, 130, 142],
    [31, 158, 137],
    [53, 183, 121],
    [109, 205, 89],
    [180, 222, 44],
    [253, 231, 37],
  ],
  inferno: [
    [0, 0, 4],
    [22, 11, 57],
    [66, 10, 104],
    [106, 23, 110],
    [147, 38, 103],
    [188, 55, 84],
    [221, 81, 58],
    [243, 120, 25],
    [252, 165, 10],
    [252, 255, 164],
  ],
  gray: [
    [0, 0, 0],
    [255, 255, 255],
  ],
};

const cache = new Map<ColormapName, Uint8ClampedArray>();

/** 256-entry RGB lookup table, 3 bytes per entry. */
export function colormap(name: ColormapName): Uint8ClampedArray {
  const cached = cache.get(name);
  if (cached !== undefined) return cached;

  const anchors = ANCHORS[name];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const position = (i / 255) * (anchors.length - 1);
    const low = Math.floor(position);
    const high = Math.min(anchors.length - 1, low + 1);
    const t = position - low;
    for (let channel = 0; channel < 3; channel++) {
      lut[i * 3 + channel] = anchors[low][channel] * (1 - t) + anchors[high][channel] * t;
    }
  }

  cache.set(name, lut);
  return lut;
}

/** A CSS gradient for the same map, so the legend cannot drift from the image. */
export function colormapGradient(name: ColormapName): string {
  const lut = colormap(name);
  const stops: string[] = [];
  for (let i = 0; i <= 10; i++) {
    const index = Math.round((i / 10) * 255) * 3;
    stops.push(`rgb(${lut[index]},${lut[index + 1]},${lut[index + 2]}) ${i * 10}%`);
  }
  return `linear-gradient(to top, ${stops.join(", ")})`;
}
