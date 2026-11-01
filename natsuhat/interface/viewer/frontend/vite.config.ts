import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// The viewer is served as static files by a stdlib Python server on an
// arbitrary ephemeral port, so all asset URLs must be relative (base: "./").
// The build output is committed at natsuhat/interface/viewer/static and shipped.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  // Set only by scripts/viewer_dev.py for its disposable demo backend.
  // These server-side variables are never exposed as VITE_* client variables.
  server: {
    proxy: process.env.NATSUHAT_VIEWER_DEV_BACKEND ? {
      "/api": {
        target: process.env.NATSUHAT_VIEWER_DEV_BACKEND,
        headers: { Cookie: process.env.NATSUHAT_VIEWER_DEV_COOKIE ?? "" },
      },
    } : undefined,
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
});
