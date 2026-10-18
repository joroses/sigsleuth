// Measures loading #map against the local API. Run via scripts/perf-map.sh,
// which rebuilds the page first so the numbers match the checkout.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { chromium, type Browser, type Request } from "@playwright/test";

const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
const resultsDir = fileURLToPath(new URL("../perf-results/", import.meta.url));
const port = process.env.SIGSLEUTH_PERF_API_PORT ?? "8123";
const origin = `http://127.0.0.1:${port}`;
const timeoutMs = 60_000;

// Table rows in display order. MapView records the "map: …" User Timing measures.
const METRICS = [
  ["nodes", "Nodes returned", "count"],
  ["apiQueryMs", "API query", "ms"],
  ["apiSerializationMs", "API serialization", "ms"],
  ["mapDataBytes", "Map data transfer", "bytes"],
  ["constructionMs", "Map construction", "ms"],
  ["dataReceivedMs", "Data received", "ms"],
  ["linksReceivedMs", "Links received", "ms"],
  ["nodeLayersIdleMs", "Node layers idle", "ms"],
  ["scriptMs", "Main-thread JS", "ms"],
  ["longTaskMs", "Long tasks", "ms"],
  ["totalBytes", "Total transfer", "bytes"],
] as const;

type Metrics = Partial<Record<(typeof METRICS)[number][0], number | null>>;

interface Results {
  generatedAt: string;
  commit: string | null;
  runs: Metrics[];
  medians: Metrics;
}

async function measureRun(
  browser: Browser,
  tracePath: string | null,
): Promise<Metrics> {
  // A fresh context each time, so the browser cache starts empty.
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  try {
    const page = await context.newPage();
    const finished: Request[] = [];
    context.on("requestfinished", (request) => finished.push(request));
    const cdp = await context.newCDPSession(page);
    await cdp.send("Performance.enable");
    // Long tasks are not kept in the performance buffer, so observe them from the start.
    await page.addInitScript(() => {
      const w = window as Window & { longTaskMs?: number };
      w.longTaskMs = 0;
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) w.longTaskMs! += entry.duration;
      }).observe({ type: "longtask" });
    });
    if (tracePath) await browser.startTracing(page, { path: tracePath });

    await page.goto(`${origin}/#map`, { timeout: timeoutMs });
    await page.waitForFunction(
      () => {
        const received = performance.getEntriesByName("map: data received");
        const idle = performance.getEntriesByName("map: node layers idle");
        return (
          (idle.at(-1)?.duration ?? 0) >= (received.at(-1)?.duration ?? 1e9)
        );
      },
      undefined,
      { timeout: timeoutMs, polling: 100 },
    );
    // Let tiles and late requests finish so byte totals are comparable.
    await page
      .waitForLoadState("networkidle", { timeout: 10_000 })
      .catch(() => {});
    if (tracePath) await browser.stopTracing();

    const fromPage = await page.evaluate(() => {
      const measure = (name: string) =>
        performance.getEntriesByName(name, "measure").at(-1)?.duration ?? null;
      const resource = (path: string) =>
        performance
          .getEntriesByType("resource")
          .findLast((e) => new URL(e.name).pathname === path) as
          PerformanceResourceTiming | undefined;
      const api = resource("/api/v1/map/nodes");
      const server = (name: string) =>
        api?.serverTiming.find((t) => t.name === name)?.duration ?? null;
      return {
        apiQueryMs: server("query"),
        apiSerializationMs: server("serialization"),
        constructionMs: measure("map: construction"),
        dataReceivedMs: measure("map: data received"),
        linksReceivedMs: resource("/api/v1/map/links")?.responseEnd ?? null,
        nodeLayersIdleMs: measure("map: node layers idle"),
        longTaskMs:
          (window as Window & { longTaskMs?: number }).longTaskMs ?? null,
      };
    });
    const { metrics } = await cdp.send("Performance.getMetrics");
    const scriptSeconds = metrics.find(
      (m) => m.name === "ScriptDuration",
    )?.value;

    // Playwright sizes include headers and cross-origin tiles, unlike resource timing.
    let totalBytes = 0;
    let mapDataBytes: number | null = null;
    let nodes: number | null = null;
    for (const request of finished) {
      const sizes = await request.sizes().catch(() => null);
      const bytes = sizes
        ? sizes.responseBodySize + sizes.responseHeadersSize
        : 0;
      totalBytes += bytes;
      if (new URL(request.url()).pathname === "/api/v1/map/nodes") {
        mapDataBytes = bytes;
        const body = await (await request.response())?.json().catch(() => null);
        nodes = Array.isArray(body?.items) ? body.items.length : null;
      }
    }
    return {
      ...fromPage,
      nodes,
      mapDataBytes,
      totalBytes,
      scriptMs: scriptSeconds === undefined ? null : scriptSeconds * 1000,
    };
  } finally {
    await context.close();
  }
}

function median(values: Array<number | null | undefined>): number | null {
  const sorted = values
    .filter((v): v is number => typeof v === "number")
    .sort((a, b) => a - b);
  if (sorted.length === 0) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function format(value: number | null | undefined, unit: string): string {
  if (value == null) return "–";
  if (unit === "ms") return `${Math.round(value)} ms`;
  if (unit === "count") return String(Math.round(value));
  if (Math.abs(value) >= 1024 * 1024)
    return `${(value / 1024 / 1024).toFixed(2)} MB`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function table(current: Metrics, baseline: Metrics | null): string {
  const rows = [
    baseline
      ? ["Metric", "Current", "Baseline", "Change"]
      : ["Metric", "Median"],
  ];
  for (const [key, label, unit] of METRICS) {
    const now = current[key];
    const before = baseline?.[key];
    let change = "";
    if (now != null && before != null) {
      const diff = now - before;
      const percent =
        before === 0
          ? ""
          : ` (${diff >= 0 ? "+" : ""}${Math.round((diff / before) * 100)}%)`;
      change = `${diff >= 0 ? "+" : "-"}${format(Math.abs(diff), unit)}${percent}`;
    }
    rows.push(
      baseline
        ? [label, format(now, unit), format(before, unit), change]
        : [label, format(now, unit)],
    );
  }
  const widths = rows[0].map((_, i) =>
    Math.max(...rows.map((r) => r[i].length)),
  );
  return rows
    .map((row) =>
      row
        .map((cell, i) =>
          i === 0 ? cell.padEnd(widths[i]) : cell.padStart(widths[i]),
        )
        .join("  ")
        .trimEnd(),
    )
    .join("\n");
}

async function startApiServer(): Promise<ChildProcess> {
  const health = () =>
    fetch(`${origin}/api/v1/status`, { signal: AbortSignal.timeout(2000) })
      .then((response) => response.ok)
      .catch(() => false);
  if (await health())
    throw new Error(
      `Port ${port} is already in use. Set SIGSLEUTH_PERF_API_PORT to a free port.`,
    );
  const server = spawn(
    "uv",
    ["run", "--locked", "python", "-m", "sigsleuth", "api"],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        SIGSLEUTH_API_HOST: "127.0.0.1",
        SIGSLEUTH_API_PORT: port,
        SIGSLEUTH_LOG_LEVEL: "WARNING",
      },
      stdio: ["ignore", "ignore", "inherit"],
    },
  );
  for (let waited = 0; waited < 60_000; waited += 250) {
    if (server.exitCode !== null)
      throw new Error("The API server exited early.");
    if (await health()) return server;
    await sleep(250);
  }
  server.kill("SIGTERM");
  throw new Error("The API server did not become healthy within 60 s.");
}

function lastCommit(): string | null {
  try {
    const git = (...args: string[]) =>
      execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" }).trim();
    const dirty = git("status", "--porcelain") ? " (uncommitted changes)" : "";
    return `${git("log", "-1", "--pretty=%h %s")}${dirty}`;
  } catch {
    return null;
  }
}

async function main(): Promise<void> {
  let runs = 3;
  let trace = false;
  let baselinePath: string | undefined;
  for (const arg of process.argv.slice(2)) {
    if (arg === "--help" || arg === "-h") {
      console.log(
        "Usage: ./scripts/perf-map.sh [runs] [baseline.json] [--trace]\n\n" +
          "Runs default to 3. The baseline defaults to the previous latest.json;\n" +
          "a path is relative to the repository root. --trace saves a Chrome\n" +
          "trace per run to open in the devtools Performance panel.\n" +
          "SIGSLEUTH_PERF_API_PORT changes the API port (default 8123).",
      );
      return;
    }
    if (arg === "--trace") trace = true;
    else if (/^\d+$/.test(arg) && Number(arg) > 0) runs = Number(arg);
    else if (!arg.startsWith("-") && !baselinePath) baselinePath = arg;
    else throw new Error(`Unknown argument: ${arg}. Use --help.`);
  }

  let baseline: Results | null = null;
  const baselineFile = baselinePath
    ? resolve(repoRoot, baselinePath)
    : `${resultsDir}latest.json`;
  try {
    baseline = JSON.parse(await readFile(baselineFile, "utf8")) as Results;
    if (!baseline.medians) baseline = null; // Written by an older version.
  } catch (error) {
    if (baselinePath)
      throw new Error(
        `Could not read baseline ${baselinePath}: ${String(error)}`,
        { cause: error },
      );
  }
  if (baselinePath && !baseline)
    throw new Error(`${baselinePath} has no medians to compare with.`);

  await mkdir(resultsDir, { recursive: true });
  const generatedAt = new Date().toISOString();
  const prefix = `${resultsDir}${generatedAt.replaceAll(":", "-")}`;
  const server = await startApiServer();
  let browser: Browser | undefined;
  const results: Metrics[] = [];
  try {
    browser = await chromium.launch();
    for (let index = 1; index <= runs; index++) {
      const run = await measureRun(
        browser,
        trace ? `${prefix}-run-${index}.trace.json` : null,
      );
      results.push(run);
      process.stderr.write(
        `Run ${index}/${runs}: data received ${format(run.dataReceivedMs, "ms")}, node layers idle ${format(run.nodeLayersIdleMs, "ms")}\n`,
      );
    }
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }

  const medians: Metrics = {};
  for (const [key] of METRICS)
    medians[key] = median(results.map((r) => r[key]));
  const report: Results = {
    generatedAt,
    commit: lastCommit(),
    runs: results,
    medians,
  };
  const json = `${JSON.stringify(report, null, 2)}\n`;
  await writeFile(`${prefix}.json`, json);
  await writeFile(`${resultsDir}latest.json`, json);

  console.log(
    `\nMap load, median of ${runs} run${runs === 1 ? "" : "s"} — ${report.commit ?? "unknown commit"}`,
  );
  if (baseline)
    console.log(
      `Baseline: ${baselinePath ?? "previous latest.json"} — ${baseline.commit ?? "unknown commit"}`,
    );
  console.log(
    `\n${table(medians, baseline?.medians ?? null)}\n\nSaved ${prefix}.json`,
  );
  if (trace) console.log(`Traces: ${prefix}-run-*.trace.json`);
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
