import { expect, test, type Page } from "@playwright/test";

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

    await expect(
      page.getByRole("button", { name: "Clear selected node" }),
    ).toBeVisible();
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

    await page.getByRole("button", { name: "Clear selected node" }).click();
    await expect(page).toHaveURL(/#map$/);
    await expect(
      page.getByRole("button", { name: "Clear selected node" }),
    ).toHaveCount(0);
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
