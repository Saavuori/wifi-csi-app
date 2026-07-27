import { defineConfig } from "vite";

// The server serves the built app from its own origin, so /api and /ws are same-origin in
// production. In dev, Vite proxies them to the Python server instead of the app needing to know
// where it is running.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8080", ws: true },
    },
  },
  build: {
    outDir: "dist",
    target: "es2022",
  },
});
