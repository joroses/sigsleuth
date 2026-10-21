import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CoverageCellDetail } from "./api";
import { CoverageCellDetails } from "./CoverageCellDetails";

afterEach(cleanup);

const cell: CoverageCellDetail = {
  level: 17,
  x: 66107,
  y: 48695,
  latitude: 41.92,
  longitude: 1.57,
  size_m: 227,
  measurements: 3,
  networks: [
    {
      spreading_factor: 11,
      bandwidth_khz: 250,
      frequency_mhz: 869.525,
      measurements: 3,
      average_snr: -12,
      average_margin: null,
    },
  ],
  first_heard_at: "2026-09-25T09:00:00Z",
  last_heard_at: "2026-09-25T10:00:00Z",
  heard_nodes: [
    {
      node_id: 0x0100001a,
      long_name: "Hilltop",
      short_name: "HILL",
      how: "gateway_link",
      candidates: 3,
      measurements: 2,
      guessed: 2,
      average_snr: 3.5,
      average_margin: null,
      channel: null,
      last_heard_at: "2026-09-25T10:00:00Z",
    },
    {
      node_id: 7,
      long_name: "Far away",
      short_name: null,
      how: "direct",
      candidates: 1,
      measurements: 1,
      guessed: 0,
      average_snr: 12.8,
      average_margin: null,
      channel: "LongFast",
      last_heard_at: "2026-09-25T09:00:00Z",
    },
  ],
  unknown_relays: [],
  newest: [],
};

describe("Coverage here", () => {
  it("chooses a heard node drawn on the map and links to one that isn't", () => {
    const choose = vi.fn();
    render(
      <CoverageCellDetails
        detail={{ data: cell, error: null, loading: false }}
        onlyOf={null}
        isDrawn={(nodeId) => nodeId !== 7}
        onChooseNode={choose}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Hilltop" }));
    expect(choose).toHaveBeenCalledWith(0x0100001a);
    expect(
      screen.getByRole("link", { name: "Far away" }).getAttribute("href"),
    ).toBe("#nodes/7");
  });
});
