export interface View {
  id: string;
  title: string;
  root: HTMLElement;
  /** Called when the view becomes visible. Subscribe here, not in the constructor. */
  mount(): void;
  /** Called when it is hidden. Must release every subscription and animation frame — a view
   *  that keeps decoding while off-screen is the easiest way to make the app feel slow. */
  unmount(): void;
}
