import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API and the UI are served from the same origin in production; in dev
// the API runs separately, so proxy /api (including the WebSocket) to it.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true, ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
