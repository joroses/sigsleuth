import { describe, expect, it } from "vitest";

import { firmwareCell } from "./nodeDisplay";

describe("firmwareCell", () => {
  it("sorts a reported version and a proven minimum on one scale", () => {
    const reported = firmwareCell({
      firmware_version: "2.7.15.567b8ea",
      firmware: {
        minimum: "2.6.8",
        reason: "its NodeInfo carries a public key",
      },
    });
    const minimum = firmwareCell({
      firmware_version: null,
      firmware: { minimum: "2.8.0", reason: "it signs its packets" },
    });
    const unknown = firmwareCell({ firmware_version: null, firmware: null });

    expect(reported).toEqual({
      text: "2.7.15",
      title: "2.7.15.567b8ea, from its map report",
      number: 20715,
    });
    expect(minimum).toEqual({
      text: "≥ 2.8.0",
      title: "2.8.0 or newer: it signs its packets",
      number: 20800,
    });
    expect(unknown).toEqual({ text: null, title: undefined, number: null });
  });
});
