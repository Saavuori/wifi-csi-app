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
 * An inline SVG glyph.
 *
 * Stroked paths on a 24-unit grid, coloured by `currentColor` so a tab that becomes active
 * carries its icon with it. Small enough that hand-writing the handful this app needs costs less
 * than a dependency that ships a thousand.
 */
export function icon(paths: string): HTMLElement {
  return el("span", {
    class: "icon",
    "aria-hidden": "true",
    html: `<svg viewBox="0 0 24 24">${paths}</svg>`,
  });
}

export const ICONS = {
  waterfall: '<path d="M5 9v6M9.7 5v14M14.3 8.5v7M19 4v16"/>',
  motion: '<path d="M3 12h3.6l2.7-7 3.6 14 2.6-7H21"/>',
  vitals:
    '<path d="M12 20.3S4.8 15.6 4.8 10.8A3.9 3.9 0 0 1 12 8.6a3.9 3.9 0 0 1 7.2 2.2c0 4.8-7.2 9.5-7.2 9.5z"/>',
  placement: '<circle cx="12" cy="12" r="3.6"/><path d="M12 2.8v3.2M12 18v3.2M2.8 12H6M18 12h3.2"/>',
  more: '<circle cx="5.5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="18.5" cy="12" r="1.4"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  expand: '<path d="M9 4H4v5M15 4h5v5M15 20h5v-5M9 20H4v-5"/>',
  collapse: '<path d="M4 9h5V4M20 9h-5V4M20 15h-5v5M4 15h5v5"/>',
} as const;

/* -- fullscreen ---------------------------------------------------------------------------- */

/**
 * Make one element fill the screen, and tell the caller when that changes.
 *
 * Two mechanisms, because one is not enough. The Fullscreen API is the right thing where it
 * exists — it takes over the whole display, including the browser chrome. On iOS it does not
 * exist for anything but a `<video>`, and a phone is precisely where a waterfall most wants the
 * extra pixels, so the fallback pins the element over the viewport with CSS instead. Both paths
 * set the same class, so the styling is written once.
 *
 * `onChange` fires for either path, including when the user leaves by pressing Escape or by a
 * gesture we never see — which callers need, because a canvas has to be resized and redrawn.
 */
export function fullscreen(target: HTMLElement, onChange: (active: boolean) => void) {
  const supported = typeof target.requestFullscreen === "function";
  let fallbackActive = false;

  // Either path counts. Selecting on `supported` instead looks equivalent and is not: the API
  // can exist and still refuse the request — an untrusted gesture, a permissions policy, an
  // iframe without `allow="fullscreen"` — and we then use the CSS path anyway. Reading only
  // `document.fullscreenElement` in that case reports "not fullscreen" a frame after we turned
  // it on, so the class is removed again and the button does nothing at all.
  const active = () => document.fullscreenElement === target || fallbackActive;

  const sync = () => {
    const on = active();
    target.classList.toggle("is-fullscreen", on);
    document.documentElement.classList.toggle("has-fullscreen", on);
    onChange(on);
  };

  if (supported) document.addEventListener("fullscreenchange", sync);

  // Escape is handled by the browser for the real thing; the fallback has to do it itself.
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape" && fallbackActive) {
      event.preventDefault();
      fallbackActive = false;
      sync();
    }
  };
  document.addEventListener("keydown", onKey);

  return {
    active,
    async toggle() {
      if (supported) {
        // A rejected request is not an error worth surfacing: it means the gesture was not
        // trusted or the element cannot go fullscreen, and the view behind it is still fine.
        try {
          if (document.fullscreenElement === target) await document.exitFullscreen();
          else await target.requestFullscreen({ navigationUI: "hide" });
          return;
        } catch {
          // Fall through to the CSS path rather than leaving the button dead.
        }
      }
      fallbackActive = !fallbackActive;
      sync();
    },
    dispose() {
      if (supported) document.removeEventListener("fullscreenchange", sync);
      document.removeEventListener("keydown", onKey);
      if (fallbackActive) {
        fallbackActive = false;
        target.classList.remove("is-fullscreen");
        document.documentElement.classList.remove("has-fullscreen");
      }
    },
  };
}

/* -- bottom sheets ------------------------------------------------------------------------ */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface SheetController {
  open(): void;
  close(): void;
  isOpen(): boolean;
}

// Every controller that is currently open. Navigating away while a sheet is up would otherwise
// leave `html.sheet-open` behind — a scroll lock with nothing left on screen to explain it.
const openSheets = new Set<SheetController>();

export function closeAllSheets() {
  for (const controller of [...openSheets]) controller.close();
}

/**
 * Wire the open/close behaviour of a bottom sheet onto elements the caller already owns.
 *
 * `marker` + `markerClass` say where the "is open" class goes, which differs between the two
 * kinds of sheet: a free-standing overlay marks itself, while a view's control column is marked
 * by its view root (so the same element can be a 320 px rail on the desktop without the class
 * meaning anything there).
 */
function attachSheet(options: {
  panel: HTMLElement;
  scrim: HTMLElement;
  marker: HTMLElement;
  markerClass: string;
  onClose?: () => void;
}): SheetController {
  const { panel, scrim, marker, markerClass } = options;
  let open = false;
  let returnFocus: HTMLElement | null = null;

  function onKeydown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      controller.close();
      return;
    }
    if (event.key !== "Tab") return;
    // Keep the keyboard inside the sheet: behind it is a live plot and a tab bar, and tabbing
    // into either while a modal surface is up leaves the focus ring somewhere invisible.
    const targets = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
      (node) => node.offsetParent !== null,
    );
    if (targets.length === 0) return;
    const first = targets[0];
    const last = targets[targets.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === panel)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const controller: SheetController = {
    isOpen: () => open,
    open() {
      if (open) return;
      open = true;
      openSheets.add(controller);
      returnFocus = document.activeElement as HTMLElement | null;
      marker.classList.add(markerClass);
      scrim.classList.add("sheet-scrim-open");
      panel.removeAttribute("aria-hidden");
      document.documentElement.classList.add("sheet-open");
      document.addEventListener("keydown", onKeydown, true);
      const first = panel.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? panel).focus({ preventScroll: true });
    },
    close() {
      if (!open) return;
      open = false;
      openSheets.delete(controller);
      marker.classList.remove(markerClass);
      scrim.classList.remove("sheet-scrim-open");
      panel.style.transform = "";
      document.documentElement.classList.remove("sheet-open");
      document.removeEventListener("keydown", onKeydown, true);
      returnFocus?.focus({ preventScroll: true });
      returnFocus = null;
      options.onClose?.();
    },
  };

  scrim.addEventListener("click", () => controller.close());

  return controller;
}

/**
 * Drag the sheet's head downwards to dismiss it.
 *
 * A sheet that can only be closed by finding a button is a sheet that gets left open over the
 * plot it was covering.
 */
function attachGrip(controller: SheetController, panel: HTMLElement, grip: HTMLElement) {
  let startY = 0;
  let dragging = false;

  grip.addEventListener("pointerdown", (event: PointerEvent) => {
    if (!controller.isOpen() || event.button !== 0) return;
    dragging = true;
    startY = event.clientY;
    grip.setPointerCapture(event.pointerId);
    panel.style.transition = "none";
  });

  grip.addEventListener("pointermove", (event: PointerEvent) => {
    if (!dragging) return;
    panel.style.transform = `translateY(${Math.max(0, event.clientY - startY)}px)`;
  });

  const end = (event: PointerEvent) => {
    if (!dragging) return;
    dragging = false;
    grip.releasePointerCapture(event.pointerId);
    panel.style.transition = "";
    const dy = event.clientY - startY;
    panel.style.transform = "";
    if (dy > 64) controller.close();
  };

  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

function sheetHead(title: string, onClose: () => void): HTMLElement {
  return el(
    "div",
    { class: "sheet-head" },
    el("h2", { class: "sheet-title" }, title),
    el("button", { class: "sheet-close", type: "button", onclick: onClose }, "Done"),
  );
}

/**
 * A free-standing bottom sheet, appended to the body on open.
 *
 * `body` is adopted rather than copied, and put back where it came from on close — which is what
 * lets a row of buttons live inline on the desktop and inside a sheet on a phone without either
 * copy of the handlers going stale.
 */
export function sheet(options: { title: string; body: Node; className?: string }): SheetController {
  const panel = el("section", {
    class: `sheet-surface ${options.className ?? ""}`,
    role: "dialog",
    "aria-modal": "true",
    "aria-label": options.title,
    tabindex: -1,
  });
  const scrim = el("div", { class: "sheet-scrim" });

  const home = { parent: options.body.parentNode, next: options.body.nextSibling };

  const controller = attachSheet({
    panel,
    scrim,
    marker: panel,
    markerClass: "sheet-shown",
    onClose: () => {
      // Hand the body back to wherever it was, then take the overlay off the document. Delayed
      // by the slide-out so the sheet does not vanish mid-animation.
      window.setTimeout(() => {
        if (controller.isOpen()) return;
        home.parent?.insertBefore(options.body, home.next);
        panel.remove();
        scrim.remove();
      }, 240);
    },
  });

  const head = sheetHead(options.title, () => controller.close());
  panel.append(head, options.body);
  attachGrip(controller, panel, head);

  return {
    isOpen: () => controller.isOpen(),
    close: () => controller.close(),
    open() {
      document.body.append(scrim, panel);
      // One frame, so the browser has a "closed" position to transition from. Appending and
      // adding the open class in the same task animates from nowhere.
      requestAnimationFrame(() => controller.open());
    },
  };
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

/**
 * A segmented control: two or three destinations that belong to one another.
 *
 * Used where a phone has one tab for what the desktop lists as several views — the Vitals tab
 * covers both bands, and the switch between them belongs inside the view rather than in a nav
 * the phone does not have room for.
 */
export function segmented(options: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  label?: string;
}): HTMLElement {
  const group = el("div", {
    class: "segmented",
    role: "group",
    "aria-label": options.label ?? "",
  });
  for (const option of options.options) {
    const active = option.value === options.value;
    group.append(
      el(
        "button",
        {
          class: `seg-button ${active ? "seg-active" : ""}`,
          type: "button",
          "aria-pressed": active ? "true" : "false",
          onclick: () => options.onChange(option.value),
        },
        option.label,
      ),
    );
  }
  return group;
}

/**
 * An explanatory paragraph, collapsed behind a toggle on a phone and simply visible on a desktop.
 *
 * The prose in this app earns its place — it is the difference between a number and a number you
 * know how to act on — but on a 360 px screen five lines of it outweigh the instrument they are
 * explaining. The CSS decides which of the two treatments applies; this only supplies both.
 */
export function hintBlock(text: string, summary = "Why this matters"): HTMLElement {
  const block = el("div", { class: "hint-block" });
  const toggle = el(
    "button",
    {
      class: "hint-toggle",
      type: "button",
      "aria-expanded": "false",
      onclick: () => {
        const open = block.classList.toggle("hint-block-open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      },
    },
    summary,
  );
  block.append(toggle, el("p", { class: "hint" }, text));
  return block;
}

/**
 * Two-column view scaffold: content on the left, controls on the right.
 *
 * Explicit columns rather than letting panels flow into a grid. With auto-flow, the column a
 * panel lands in depends on how many panels came before it, so adding a summary panel at the
 * top of a view silently pushes the main plot into the 320px control column.
 *
 * On a phone that second column becomes a bottom sheet, opened by the Tune button this adds.
 * It is the same element and the same DOM either way — the media query does the switching — so
 * a view gets the sheet without knowing it exists, and the desktop rail is unaffected.
 */
export function viewLayout(options: {
  main: (Node | null)[];
  side?: (Node | null)[];
  className?: string;
  controlsTitle?: string;
}): HTMLElement {
  const root = el("div", {
    class: `view ${options.side?.length ? "view-split" : ""} ${options.className ?? ""}`,
  });
  root.append(el("div", { class: "col col-main" }, ...options.main));

  if (options.side === undefined || options.side.length === 0) return root;

  const title = options.controlsTitle ?? "Controls";
  const scrim = el("div", { class: "sheet-scrim" });
  // Deliberately not `role="dialog" aria-modal`: this is the same element the desktop lays out
  // as an ordinary 320 px rail, and announcing a rail as a modal dialog would be a lie in the
  // one layout where it is always on screen.
  const side = el("div", { class: "col col-side", "aria-label": title, tabindex: -1 });

  const controller = attachSheet({
    panel: side,
    scrim,
    marker: root,
    markerClass: "view-sheet-open",
  });

  const head = sheetHead(title, () => controller.close());
  side.append(head, ...options.side.filter((node): node is Node => node !== null));
  attachGrip(controller, side, head);

  root.append(
    side,
    scrim,
    el(
      "button",
      { class: "tune", type: "button", onclick: () => controller.open() },
      icon(ICONS.sliders),
      title,
    ),
  );
  return root;
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
