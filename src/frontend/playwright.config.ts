import { defineConfig, devices } from "@playwright/test";

// SIGSLEUTH_E2E_PORT moves the tests off the default port when it is taken.
const port = Number(process.env.SIGSLEUTH_E2E_PORT ?? 4173);
// Clear of Vite's own default port, 5173.
const apiPort = port + 2000;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      // The API on the recorded sample; a new sample takes a few seconds more
      // to capture.
      command: `uv run --locked python ../../tests/serve_sample.py ${apiPort}`,
      url: `http://127.0.0.1:${apiPort}/api/v1/status`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `corepack pnpm exec vite --host 127.0.0.1 --port ${port}`,
      url: `http://127.0.0.1:${port}`,
      reuseExistingServer: false,
      env: { SIGSLEUTH_API_PORT: String(apiPort) },
    },
  ],
});
