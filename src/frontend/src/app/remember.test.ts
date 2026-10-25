import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DEFAULT_FILTER } from "./mapNodeFilter";
import { loadSetting, useLinkedSetting, useSetting } from "./remember";

afterEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "");
});

function kept(key: string, initial: unknown, saved: string | null) {
  if (saved !== null) localStorage.setItem(`sigsleuth.${key}`, saved);
  return renderHook(() => useSetting(key, initial)).result.current[0];
}

describe("useSetting", () => {
  it("keeps the saved fields of an object and fills the rest from the default", () => {
    // Saved before the filter had `estimated` and the "other" category.
    const saved = JSON.stringify({
      categories: { relay: false, gateway: true },
      role: "ROUTER",
      channel: 7,
      gone: true,
    });
    expect(kept("nodeFilter", DEFAULT_FILTER, saved)).toEqual({
      ...DEFAULT_FILTER,
      categories: { relay: false, gateway: true, other: true },
      role: "ROUTER",
    });
  });

  it("starts from the default when nothing usable is kept", () => {
    expect(kept("nodeFilter", DEFAULT_FILTER, null)).toEqual(DEFAULT_FILTER);
    expect(kept("nodeFilter", DEFAULT_FILTER, "not json")).toEqual(
      DEFAULT_FILTER,
    );
    expect(kept("nodeFilter", DEFAULT_FILTER, '"ROUTER"')).toEqual(
      DEFAULT_FILTER,
    );
    expect(kept("coverage", false, '"yes"')).toBe(false);
  });
});

describe("useLinkedSetting", () => {
  it("shows a link's value without keeping it, and keeps a choice made after", () => {
    localStorage.setItem("sigsleuth.coverage", "false");
    const linked = renderHook(() => useLinkedSetting("coverage", false, true));
    expect(linked.result.current[0]).toBe(true);
    expect(loadSetting("coverage")).toBe(false);

    act(() => linked.result.current[1](true));
    expect(loadSetting("coverage")).toBe(true);
  });

  it("shows the link's value again on Back, until a choice replaces it", () => {
    renderHook(() => useLinkedSetting("coverage", false, true)).unmount();
    // Back to the entry, whose address may have lost the link's query.
    const back = renderHook(() =>
      useLinkedSetting("coverage", false, undefined),
    );
    expect(back.result.current[0]).toBe(true);
    expect(loadSetting("coverage")).toBeUndefined();

    act(() => back.result.current[1](false));
    back.unmount();
    // Back again, even with the query: the choice shows, not the link's value.
    expect(
      renderHook(() => useLinkedSetting("coverage", false, true)).result
        .current[0],
    ).toBe(false);
  });
});
