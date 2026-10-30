import { describe, expect, it } from "vitest";

import { fadeMargin, linkTier } from "./linkQuality";

describe("linkTier", () => {
  it("leaves signal quality unknown without radio settings", () => {
    expect(fadeMargin(-4, null)).toBeNull();
    expect(linkTier(-4, null)).toBe("unknown");
  });
  it("judges SNR against the spreading factor's floor", () => {
    // SF7 is good from 2.5 dB and fair from -3.5 dB; SF11 from -7.5 and -13.5 dB.
    expect(linkTier(2.5, 7)).toBe("good");
    expect(linkTier(2, 7)).toBe("fair");
    expect(linkTier(-4, 7)).toBe("marginal");
    expect(linkTier(-4, 11)).toBe("good");
    expect(linkTier(-13.5, 11)).toBe("fair");
    expect(linkTier(-14, 11)).toBe("marginal");
  });
});
