import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type ShownWindow,
  SHOWN_WINDOW_VIEW,
  mapUrls,
  startMapRequests,
} from "./mapTimeFilter";
import { readView } from "./remember";
import { forgetKept, useApi } from "./useApi";

afterEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "");
  vi.unstubAllGlobals();
  forgetKept();
});

function stubFetch() {
  const fetchMock = vi.fn<(url: string) => Promise<Response>>(() =>
    Promise.resolve(
      new Response(JSON.stringify({ items: [], truncated: false })),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("startMapRequests", () => {
  it("asks for the saved window and link layers, which the map then reuses", async () => {
    localStorage.setItem("sigsleuth.seenWindow", JSON.stringify("24h"));
    localStorage.setItem(
      "sigsleuth.linkLayers",
      JSON.stringify({ traceroute: false, direct: true }),
    );
    const fetchMock = stubFetch();

    startMapRequests();

    const shown = readView(SHOWN_WINDOW_VIEW) as ShownWindow;
    expect(shown.window).toBe("24h");
    const urls = mapUrls(shown.window, shown.chosenAt);
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      urls.nodes,
      urls.textSenders,
      urls.direct,
    ]);
    const { result } = renderHook(() => useApi(urls.nodes, shown.chosenAt));
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("keeps the window a history entry already loaded", () => {
    const kept: ShownWindow = { window: "7d", chosenAt: 1_000_000 };
    window.history.replaceState({ [SHOWN_WINDOW_VIEW]: kept }, "");
    const fetchMock = stubFetch();

    startMapRequests();

    expect(fetchMock.mock.calls[0]?.[0]).toBe(mapUrls("7d", 1_000_000).nodes);
  });

  it("prefetches a shared view's window and layers instead of saved choices", () => {
    localStorage.setItem("sigsleuth.seenWindow", JSON.stringify("1h"));
    localStorage.setItem(
      "sigsleuth.linkLayers",
      JSON.stringify({ traceroute: true, direct: false, neighbor: false }),
    );
    const fetchMock = stubFetch();

    startMapRequests({
      show: ["direct", "neighbor"],
      window: "all",
      channel: null,
    });

    const shown = readView(SHOWN_WINDOW_VIEW) as ShownWindow;
    expect(shown.window).toBe("all");
    const urls = mapUrls(shown.window, shown.chosenAt);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      urls.nodes,
      urls.textSenders,
      urls.direct,
      urls.neighbor,
    ]);
  });
});
