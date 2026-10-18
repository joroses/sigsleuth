import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DEFAULT_FILTER } from "./mapNodeFilter";
import { useSetting } from "./remember";

afterEach(() => localStorage.clear());

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
