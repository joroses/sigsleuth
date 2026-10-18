import { readFileSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vitest/config";

// MapLibre GL JS v6 resolves its web worker relative to import.meta.url, which
// points into dist/assets after bundling. Emit the worker and its shared chunk
// there under their original names so the browser can fetch them.
function maplibreWorkerAssets(): Plugin {
  const workerFiles = [
    "maplibre-gl-worker.mjs",
    "maplibre-gl-shared.mjs",
  ] as const;
  return {
    name: "maplibre-worker-assets",
    apply: "build",
    generateBundle() {
      for (const file of workerFiles) {
        this.emitFile({
          type: "asset",
          fileName: `assets/${file}`,
          source: readFileSync(
            new URL(`./node_modules/maplibre-gl/dist/${file}`, import.meta.url),
          ),
        });
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), maplibreWorkerAssets()],
  // The page's stopped clock (clock.ts); nothing else from the environment.
  envPrefix: ["VITE_", "SIGSLEUTH_FIXED_NOW"],
  optimizeDeps: {
    exclude: ["maplibre-gl"],
  },
  server: {
    proxy: {
      "/api": `http://127.0.0.1:${process.env.SIGSLEUTH_API_PORT ?? 8000}`,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/testSetup.ts"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});
