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
      measurements: 2,
      average_snr: 3.5,
      average_margin: null,
      average_rssi: null,
      channel: null,
      last_heard_at: "2026-09-25T10:00:00Z",
    },
    {
      node_id: 7,
      long_name: "Far away",
      short_name: null,
      how: "direct",
      measurements: 1,
      average_snr: 12.8,
      average_margin: null,
      average_rssi: -97,
      channel: "LongFast",
      last_heard_at: "2026-09-25T09:00:00Z",
    },
  ],
  unknown_relays: [],
  newest: [],
};

describe("Coverage here", () => {
  it("chooses each unknown relay hint", () => {
    const choose = vi.fn();
    const candidates = [
      {
        node_id: 0x0200001a,
        long_name: "Relay A",
        short_name: "A",
        km: 12,
        estimated: false,
      },
      {
        node_id: 0x0300001a,
        long_name: "Relay B",
        short_name: "B",
        km: 20,
        estimated: true,
      },
    ];
    render(
      <CoverageCellDetails
        detail={{
          data: {
            ...cell,
            heard_nodes: [],
            unknown_relays: [{ relay_byte: 0x1a, measurements: 3, candidates }],
          },
          error: null,
          loading: false,
        }}
        isDrawn={() => true}
        onChooseNode={choose}
      />,
    );

    // No km to a position only estimated from the node's links.
    expect(screen.getByText(/12 km/)).toBeTruthy();
    expect(screen.queryByText(/20 km/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Relay A" }));
    expect(choose).toHaveBeenCalledWith(0x0200001a);
    choose.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Relay B" }));
    expect(choose).toHaveBeenCalledWith(0x0300001a);
  });

  it("chooses a heard node drawn on the map and links to one that isn't", () => {
    const choose = vi.fn();
    render(
      <CoverageCellDetails
        detail={{ data: cell, error: null, loading: false }}
        isDrawn={(nodeId) => nodeId !== 7}
        onChooseNode={choose}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Hilltop" }));
    expect(choose).toHaveBeenCalledWith(0x0100001a);
    expect(
      screen.getByRole("link", { name: "Far away" }).getAttribute("href"),
    ).toBe("#nodes/7");
  });

  it("shows a heard node's average RSSI when its radios recorded one", () => {
    render(
      <CoverageCellDetails
        detail={{ data: cell, error: null, loading: false }}
        isDrawn={() => true}
        onChooseNode={() => {}}
      />,
    );

    expect(screen.getByText(/12\.8 dB SNR -97 dBm/)).toBeTruthy();
    expect(screen.queryByText(/3\.5 dB SNR .*dBm/)).toBeNull();
  });
});
