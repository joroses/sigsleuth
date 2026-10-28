import { expect, test, type Page } from "@playwright/test";
import type { MapLink, MapNode } from "../src/app/api";

// The API serves the recorded sample (tests/serve_sample.py) with its clock
// stopped. These tests check page loading, map timings and node links;
// App.test.tsx and the backend's sample tests check the rest of what they show.

test.beforeEach(async ({ page }) => {
  // An empty basemap, so the tests need no network.
  await page.route("https://tiles.openfreemap.org/**", (route) =>
    route.fulfill({ json: { version: 8, sources: {}, layers: [] } }),
  );
});

test("every page loads without errors", async ({ page, request }) => {
  const first = async (url: string, items: string) =>
    (await (await request.get(url)).json())[items][0];
  const node = (await first("/api/v1/nodes?sort=traceroute&limit=1", "items"))
    .node_id;
  const packet = (await first("/api/v1/packets?limit=1&port=70", "packets"))
    .packet_row_id;
  // The page's clock stops with the API's, so its windows hold data.
  const summary = await (
    await request.get("/api/v1/diagnostics/summary")
  ).json();
  await page.clock.setFixedTime(
    Date.parse(summary.database.newest_packet_at) + 60_000,
  );
  const errors = collectErrors(page);

  for (const hash of [
    "#map",
    `#map/${node}`,
    "#zones",
    "#chat",
    "#nodes",
    `#nodes/${node}`,
    `#packets/${packet}`,
    "#links",
    "#links/traceroutes",
    "#links/line-of-sight",
    "#diagnostics",
    "#diagnostics/doubtful",
    "#diagnostics/links",
    "#diagnostics/relays",
    "#diagnostics/packets",
  ]) {
    await page.goto(`/${hash}`, { waitUntil: "networkidle" });
    await expect(page.locator(".app-content"), hash).not.toBeEmpty();
    await expect(page.getByRole("alert"), hash).toHaveCount(0);
    expect(errors, hash).toEqual([]);
  }
});

test("the map records the timings perf-map.sh waits for", async ({ page }) => {
  await page.goto("/#map");
  await expect
    .poll(() =>
      page.evaluate(() =>
        performance.getEntriesByType("measure").map((entry) => entry.name),
      ),
    )
    .toEqual(
      expect.arrayContaining([
        "map: construction",
        "map: data received",
        "map: node layers idle",
      ]),
    );
});

for (const inWindow of [true, false]) {
  test(`a node link stays selected and centers with prefetched nodes (${inWindow ? "in" : "outside"} the window)`, async ({
    page,
    request,
  }) => {
    const summary = await (
      await request.get("/api/v1/diagnostics/summary")
    ).json();
    await page.clock.setFixedTime(
      Date.parse(summary.database.newest_packet_at) + 60_000,
    );
    const { items } = await (await request.get("/api/v1/map/nodes")).json();
    const node = items[0];
    if (!inWindow) {
      await page.route("**/api/v1/map/nodes?*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        await route.fulfill({
          response,
          json: {
            ...body,
            items: body.items.filter(
              (item: { node_id: number }) => item.node_id !== node.node_id,
            ),
          },
        });
      });
    }
    await page.addInitScript(() => {
      localStorage.setItem(
        "sigsleuth.nodeFilter",
        JSON.stringify({
          categories: { relay: false, gateway: false, other: false },
        }),
      );
      localStorage.setItem(
        "sigsleuth.map.camera",
        JSON.stringify({ center: [0, 20], zoom: 1 }),
      );
    });
    const nodesReceived = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/v1/map/nodes",
    );
    await page.route("**/src/app/MapView.tsx*", async (route) => {
      await (await nodesReceived).finished();
      await route.continue();
    });

    await page.goto(`/#map/${node.node_id}`);

    await expect(page.getByRole("button", { name: /^Back to / })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`#map/${node.node_id}$`));
    await expect
      .poll(() =>
        page.evaluate(() => {
          const center = window.sigsleuthMap?.getCenter();
          // The map's projection round trip adds float noise.
          return center ? [center.lng.toFixed(6), center.lat.toFixed(6)] : null;
        }),
      )
      .toEqual([node.longitude.toFixed(6), node.latitude.toFixed(6)]);

    await page.getByRole("button", { name: /^Back to / }).click();
    await expect(page).toHaveURL(/#map$/);
    await expect(page.getByRole("button", { name: /^Back to / })).toHaveCount(
      0,
    );
  });
}

for (const { width, linked } of [
  { width: 1280, linked: false },
  { width: 390, linked: false },
  { width: 390, linked: true },
]) {
  test(`a ${linked ? "node link waits for and" : "node click"} fits its visible traceroute links (${width}px)`, async ({
    page,
    request,
  }) => {
    await page.setViewportSize({ width, height: 844 });
    const { items }: { items: MapNode[] } = await (
      await request.get("/api/v1/map/nodes")
    ).json();
    const { items: links }: { items: MapLink[] } = await (
      await request.get("/api/v1/map/links?link_kind=traceroute")
    ).json();
    const placed = new Map(items.map((node) => [node.node_id, node]));
    const link = links.find(
      (link) =>
        placed.has(link.node_a) &&
        placed.has(link.node_b) &&
        link.a_to_b.observations + link.b_to_a.observations >= 10,
    )!;
    // Spread a sample link beyond the old fixed view; the sample is local.
    const node = { ...placed.get(link.node_a)!, longitude: 0, latitude: 40 };
    const other = {
      ...placed.get(link.node_b)!,
      longitude: width === 390 ? 3 : 8,
      latitude: 40,
    };
    const hidden = {
      ...items.find(
        (item) => ![link.node_a, link.node_b].includes(item.node_id),
      )!,
      longitude: -50,
      latitude: 40,
    };
    await page.route("**/api/v1/map/nodes?*", (route) =>
      route.fulfill({
        json: { items: [node, other, hidden], truncated: false },
      }),
    );
    let release = () => {};
    const waiting = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/api/v1/map/links?*", async (route) => {
      if (linked) await waiting;
      await route.fulfill({
        json: {
          items: [
            link,
            {
              ...link,
              node_b: hidden.node_id,
              a_to_b: { ...link.a_to_b, observations: 0 },
              b_to_a: { ...link.b_to_a, observations: 0 },
            },
          ],
          truncated: false,
        },
      });
    });
    await page.addInitScript(() => {
      localStorage.setItem("sigsleuth.seenWindow", JSON.stringify("all"));
      localStorage.setItem("sigsleuth.textReach", "false");
      localStorage.setItem(
        "sigsleuth.nodeFilter",
        JSON.stringify({ channel: "*" }),
      );
      localStorage.setItem("sigsleuth.filterPanelOpen", "false");
      localStorage.setItem(
        "sigsleuth.map.camera",
        JSON.stringify({ center: [0, 40], zoom: 10 }),
      );
    });
    await page.goto(
      linked ? `/#map/${node.node_id}?show=traceroute&window=all` : "/#map",
    );
    if (!linked) {
      await expect
        .poll(() =>
          page.evaluate((nodeId) => {
            const map = window.sigsleuthMap;
            return (
              map?.getLayer("nodes") !== undefined &&
              map
                .queryRenderedFeatures({ layers: ["nodes"] })
                .some((feature) => feature.properties.nodeId === nodeId)
            );
          }, node.node_id),
        )
        .toBe(true);
      const point = await page.evaluate(() => {
        const map = window.sigsleuthMap!;
        const { x, y } = map.project([0, 40]);
        const box = map.getContainer().getBoundingClientRect();
        return { x: box.left + x, y: box.top + y };
      });
      await page.mouse.click(point.x, point.y);
    }
    await expect(page.getByRole("button", { name: /^Back to / })).toBeVisible();
    release();
    await expect
      .poll(() =>
        page.evaluate(
          (points) => {
            const map = window.sigsleuthMap;
            if (map === undefined || map.isMoving()) return false;
            const box = map.getContainer().getBoundingClientRect();
            const sheet = document
              .querySelector(".map-sidebar")!
              .getBoundingClientRect();
            const bottom =
              window.innerWidth <= 700 ? sheet.top - box.top : box.height;
            return (
              map.getZoom() >= 5 &&
              points.every(([lng, lat]) => {
                const { x, y } = map.project([lng, lat]);
                return (
                  x >= 40 && x <= box.width - 40 && y >= 40 && y <= bottom - 40
                );
              })
            );
          },
          [
            [node.longitude, node.latitude],
            [other.longitude, other.latitude],
          ],
        ),
      )
      .toBe(true);
  });
}

for (const { width, late, failed, kind, entry, show } of [
  {
    width: 1280,
    late: "reach",
    failed: null,
    kind: "traceroute",
    entry: "shared",
    show: "traceroute,reach,coverage",
  },
  {
    width: 390,
    late: "links",
    failed: null,
    kind: "direct",
    entry: "shared",
    show: "direct,reach,coverage",
  },
  {
    width: 390,
    late: "coverage",
    failed: "coverage",
    kind: "neighbor",
    entry: "shared",
    show: "neighbor,reach,coverage",
  },
  {
    width: 1280,
    late: "reach",
    failed: "reach",
    kind: "direct",
    entry: "shared",
    show: "direct,reach,coverage",
  },
  {
    width: 390,
    late: "links",
    failed: "links",
    kind: "traceroute",
    entry: "shared",
    show: "traceroute,reach,coverage",
  },
  {
    width: 1280,
    late: "reach",
    failed: null,
    kind: "traceroute",
    entry: "chatty",
    show: "traceroute,reach",
  },
  {
    width: 390,
    late: "links",
    failed: null,
    kind: "direct",
    entry: "chatty",
    show: "direct,reach,coverage",
  },
  {
    width: 1280,
    late: "reach",
    failed: null,
    kind: "neighbor",
    entry: "map",
    show: "neighbor,reach",
  },
  {
    width: 390,
    late: "links",
    failed: "links",
    kind: "traceroute",
    entry: "search",
    show: "traceroute,reach,coverage",
  },
  {
    width: 1280,
    late: "coverage",
    failed: null,
    kind: "direct",
    entry: "heard",
    show: "direct,coverage",
  },
  {
    width: 1280,
    late: "reach",
    failed: "reach",
    kind: "neighbor",
    entry: "map",
    show: "neighbor,reach,coverage",
  },
  {
    width: 390,
    late: "coverage",
    failed: "coverage",
    kind: "direct",
    entry: "chatty",
    show: "reach,coverage",
  },
  {
    width: 1280,
    late: "coverage",
    failed: null,
    kind: "traceroute",
    entry: "search",
    show: "reach,coverage",
  },
  {
    width: 390,
    late: "links",
    failed: null,
    kind: "traceroute",
    entry: "chatty",
    show: "traceroute,direct,neighbor,reach,coverage",
  },
  {
    width: 1280,
    late: "reach",
    failed: null,
    kind: "traceroute",
    entry: "unplaced",
    show: "traceroute,direct,neighbor,reach,coverage",
  },
  {
    width: 390,
    late: "coverage",
    failed: null,
    kind: "traceroute",
    entry: "heard-unplaced",
    show: "traceroute,direct,neighbor,coverage",
  },
  {
    width: 390,
    late: "coverage",
    failed: null,
    kind: "traceroute",
    entry: "search",
    show: "coverage",
  },
]) {
  test(`a ${entry} selection fits ${show} (${width}px, ${late} last, ${failed ?? "none"} fails)`, async ({
    page,
    request,
  }) => {
    const unplaced = entry === "unplaced" || entry === "heard-unplaced";
    const fromCoverage = entry === "heard" || entry === "heard-unplaced";
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width, height: 844 });
    const { items }: { items: MapNode[] } = await (
      await request.get("/api/v1/map/nodes")
    ).json();
    const { items: links }: { items: MapLink[] } = await (
      await request.get(`/api/v1/map/links?link_kind=${kind}`)
    ).json();
    // The sample has no survey spanning a node's text gateways and links.
    const node = { ...items[0], longitude: 0, latitude: 40 };
    if (unplaced)
      await page.route(`**/api/v1/nodes/${node.node_id}`, (route) =>
        route.fulfill({
          json: {
            node: { ...node, position: null, estimate: null },
            ports: [],
          },
        }),
      );
    const gateway = { ...items[1], longitude: 1, latitude: 40.5 };
    const peer = { ...items[2], longitude: -1.2, latitude: 40.2 };
    const peers = {
      traceroute: peer,
      direct: { ...items[3], longitude: -0.8, latitude: 40.8 },
      neighbor: { ...items[4], longitude: -0.5, latitude: 39.6 },
    };
    const extent = { west: -0.2, south: 39.9, east: 0.1, north: 40.1 };
    await page.route("**/api/v1/map/nodes?*", (route) =>
      route.fulfill({
        json: {
          items: [
            ...(unplaced ? [] : [node]),
            gateway,
            ...Object.values(peers),
          ],
          truncated: false,
        },
      }),
    );
    const enabled = show.split(",");
    await page.route("**/api/v1/map/text-senders*", (route) =>
      route.fulfill({
        json: {
          items: [{ ...node, messages: 3, region: null }],
          truncated: false,
        },
      }),
    );
    await page.route("**/api/v1/coverage/nodes?*", (route) =>
      route.fulfill({
        json: {
          nodes: [{ ...node, cells: 1, average_margin: 10 }],
          unknown: 0,
          truncated: false,
        },
      }),
    );
    await page.addInitScript(
      ({ enabled, fromCoverage }) => {
        localStorage.setItem("sigsleuth.seenWindow", JSON.stringify("all"));
        localStorage.setItem(
          "sigsleuth.nodeFilter",
          JSON.stringify({ channel: "*" }),
        );
        localStorage.setItem("sigsleuth.filterPanelOpen", "false");
        localStorage.setItem(
          "sigsleuth.map.camera",
          JSON.stringify({ center: [0, 40], zoom: 10 }),
        );
        localStorage.setItem(
          "sigsleuth.textReach",
          JSON.stringify(enabled.includes("reach")),
        );
        localStorage.setItem(
          "sigsleuth.coverage",
          JSON.stringify(!fromCoverage && enabled.includes("coverage")),
        );
        localStorage.setItem(
          "sigsleuth.linkLayers",
          JSON.stringify({
            traceroute: enabled.includes("traceroute"),
            direct: enabled.includes("direct"),
            neighbor: enabled.includes("neighbor"),
          }),
        );
      },
      { enabled, fromCoverage },
    );
    let release = () => {};
    const waiting = new Promise<void>((resolve) => {
      release = resolve;
    });
    const answers = {
      reach: {
        node_id: node.node_id,
        messages: 3,
        channel: node.channel,
        gateways: [{ node_id: gateway.node_id, receptions: 3 }],
      },
      coverage: extent,
      links: {
        items: [
          {
            ...links.find(
              (link) =>
                link.a_to_b.observations + link.b_to_a.observations >= 10,
            )!,
            node_a: unplaced ? gateway.node_id : node.node_id,
            node_b: peer.node_id,
          },
        ],
        truncated: false,
      },
    };
    for (const layer of ["reach", "coverage", "links"] as const) {
      const url =
        layer === "reach"
          ? `**/api/v1/map/text-reach/${node.node_id}*`
          : layer === "coverage"
            ? "**/api/v1/coverage/extent?*"
            : "**/api/v1/map/links?*";
      await page.route(url, async (route) => {
        if (layer === late) await waiting;
        const answer =
          layer === "links"
            ? {
                ...answers.links,
                items: answers.links.items.map((link) => ({
                  ...link,
                  node_b:
                    peers[
                      new URL(route.request().url()).searchParams.get(
                        "link_kind",
                      ) as keyof typeof peers
                    ].node_id,
                })),
              }
            : answers[layer];
        await route.fulfill({
          status: layer === failed ? 503 : 200,
          json:
            layer === failed
              ? { error: { code: "unavailable", message: "Unavailable" } }
              : answer,
        });
      });
    }
    const early =
      late === "links"
        ? ["reach", "coverage"]
        : ["links", late === "reach" ? "coverage" : "reach"];
    const first = Promise.all(
      early
        .filter((layer) =>
          layer === "links" ? enabled.includes(kind) : enabled.includes(layer),
        )
        .map((layer) =>
          page.waitForResponse((response) =>
            response
              .url()
              .includes(
                layer === "reach"
                  ? "/map/text-reach/"
                  : layer === "coverage"
                    ? "/coverage/extent?"
                    : "/map/links?",
              ),
          ),
        ),
    );
    const selectedCells =
      unplaced && enabled.includes("coverage")
        ? page.waitForRequest((request) => {
            const url = new URL(request.url());
            return (
              url.pathname === "/api/v1/coverage/cells" &&
              url.searchParams.get("node") === String(node.node_id)
            );
          })
        : null;
    await page.goto(
      entry === "shared"
        ? `/#map/${node.node_id}?show=${show}&window=all`
        : "/#map",
    );
    if (entry === "chatty" || fromCoverage || unplaced) {
      if (width <= 700)
        await page.getByRole("button", { name: "Expand map sidebar" }).click();
      if (fromCoverage)
        await page.getByRole("tab", { name: /Coverage/ }).click();
      await page
        .getByRole("region", {
          name: fromCoverage ? "Coverage" : "Chatty nodes",
        })
        .getByRole("button")
        .filter({ has: page.locator(".chatty-name") })
        .click();
    } else if (entry === "search") {
      if (width <= 700)
        await page.getByRole("button", { name: "Expand map sidebar" }).click();
      await page
        .getByRole("combobox", { name: "Search nodes" })
        .fill(node.node_id.toString(16));
      await page.getByRole("option").click();
    } else if (entry === "map") {
      await expect
        .poll(() =>
          page.evaluate((nodeId) => {
            const map = window.sigsleuthMap;
            return (
              map?.getLayer("nodes") !== undefined &&
              map
                .queryRenderedFeatures({ layers: ["nodes"] })
                .some((feature) => feature.properties.nodeId === nodeId)
            );
          }, node.node_id),
        )
        .toBe(true);
      const point = await page.evaluate(() => {
        const map = window.sigsleuthMap!;
        const { x, y } = map.project([0, 40]);
        const box = map.getContainer().getBoundingClientRect();
        return { x: box.left + x, y: box.top + y };
      });
      await page.mouse.click(point.x, point.y);
    }
    await expect(page.getByRole("button", { name: /^Back to / })).toBeVisible();
    await first;
    release();
    await selectedCells;

    const points = unplaced ? [] : [[node.longitude, node.latitude]];
    if (!unplaced && failed !== "links")
      for (const [kind, peer] of Object.entries(peers))
        if (enabled.includes(kind))
          points.push([peer.longitude, peer.latitude]);
    if (enabled.includes("reach") && failed !== "reach")
      points.push([gateway.longitude, gateway.latitude]);
    if (enabled.includes("coverage") && failed !== "coverage")
      points.push([extent.west, extent.south], [extent.east, extent.north]);
    if (unplaced) {
      await expect
        .poll(() =>
          page.evaluate(() => {
            const map = window.sigsleuthMap;
            if (map === undefined || !map.loaded()) return -1;
            return map.queryRenderedFeatures({
              layers: [
                "links-traceroute",
                "links-traceroute-no-snr",
                "links-direct",
                "links-direct-no-snr",
                "links-neighbor",
                "links-neighbor-no-snr",
                "links-zero-cost",
              ],
            }).length;
          }),
        )
        .toBe(0);
    }
    await expect
      .poll(() =>
        page.evaluate((points) => {
          const map = window.sigsleuthMap;
          if (map === undefined || map.isMoving()) return false;
          const canvas = map.getContainer().getBoundingClientRect();
          const sheet = document
            .querySelector(".map-sidebar")!
            .getBoundingClientRect();
          const bottom =
            window.innerWidth <= 700 ? sheet.top - canvas.top : canvas.height;
          return points.every(([lng, lat]) => {
            const { x, y } = map.project([lng, lat]);
            return (
              x >= 40 && x <= canvas.width - 40 && y >= 40 && y <= bottom - 40
            );
          });
        }, points),
      )
      .toBe(true);
  });
}

function collectErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}
