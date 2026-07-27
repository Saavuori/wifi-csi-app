// Small DOM helpers. Not a framework — the views are mostly canvas, and the parts that are not
// are simple enough that a render loop would cost more than it saves.

type Attrs = Record<string, string | number | boolean | EventListener | undefined>;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Node | string | null | undefined)[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
    } else if (key === "class") {
      node.className = String(value);
    } else if (key === "html") {
      node.innerHTML = String(value);
    } else {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

export function clear(node: Element) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/**
 * A labelled slider that reports its value continuously.
 *
 * Continuously matters for the parameters here: the plan's whole argument about window length
 * is that you should be able to *see* the estimate degrade as you drag it, and a control that
 * only commits on release hides exactly that.
 */
export function slider(options: {
  label: string;
  min: number;
  max: number;
  step: number;
  value: number;
  format?: (value: number) => string;
  onInput: (value: number) => void;
  hint?: string;
}): HTMLElement {
  const format = options.format ?? ((v: number) => String(v));
  const readout = el("span", { class: "slider-value" }, format(options.value));
  const input = el("input", {
    type: "range",
    min: options.min,
    max: options.max,
    step: options.step,
    value: options.value,
    oninput: (event: Event) => {
      const value = Number((event.target as HTMLInputElement).value);
      readout.textContent = format(value);
      options.onInput(value);
    },
  });

  return el(
    "label",
    { class: "control", title: options.hint ?? "" },
    el("span", { class: "control-label" }, options.label, readout),
    input,
  );
}

export function toggle(options: {
  label: string;
  value: boolean;
  onChange: (value: boolean) => void;
  hint?: string;
}): HTMLElement {
  const input = el("input", {
    type: "checkbox",
    checked: options.value,
    onchange: (event: Event) => options.onChange((event.target as HTMLInputElement).checked),
  });
  return el("label", { class: "control control-toggle", title: options.hint ?? "" }, input, options.label);
}

export function select(options: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  hint?: string;
}): HTMLElement {
  const node = el("select", {
    onchange: (event: Event) => options.onChange((event.target as HTMLSelectElement).value),
  });
  for (const option of options.options) {
    const child = el("option", { value: option.value }, option.label);
    if (option.value === options.value) child.selected = true;
    node.append(child);
  }
  return el(
    "label",
    { class: "control", title: options.hint ?? "" },
    el("span", { class: "control-label" }, options.label),
    node,
  );
}

export function stat(label: string, value: string, className = ""): HTMLElement {
  return el(
    "div",
    { class: `stat ${className}` },
    el("div", { class: "stat-value" }, value),
    el("div", { class: "stat-label" }, label),
  );
}

/**
 * Size a canvas to its CSS box at device pixel ratio.
 *
 * Returns true if the backing store changed, which callers use as "history is gone, redraw
 * from scratch" — resizing a canvas clears it, and the waterfall's history lives in the canvas
 * itself.
 */
export function fitCanvas(canvas: HTMLCanvasElement): boolean {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.floor(canvas.clientHeight * ratio));
  if (canvas.width === width && canvas.height === height) return false;
  canvas.width = width;
  canvas.height = height;
  return true;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}
