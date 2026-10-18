import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
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
      spreading_factor: 7,
      bandwidth_khz: 62.5,
      frequency_mhz: 869.618,
      measurements: 2,
      average_snr: 6.75,
      average_margin: null,
    },
    {
      spreading_factor: 11,
      bandwidth_khz: 250,
      frequency_mhz: 869.525,
      measurements: 1,
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
  unknown_relays: [
    {
      relay_byte: 0x2b,
      measurements: 4,
      candidates: [
        { node_id: 0x0200002b, long_name: "Ridge", short_name: null, km: 3.2 },
        { node_id: 0x0300002b, long_name: null, short_name: "VLY", km: 12 },
      ],
    },
  ],
  newest: [
    {
      heard_at: "2026-09-25T10:00:00Z",
      heard_node_id: 0x0100001a,
      heard_name: "Hilltop",
      heard_how: "gateway_link",
      relay_byte: 0x1a,
      rx_snr: 3.5,
      rx_rssi: -101,
      margin: 11,
      packet_row_id: null,
    },
    {
      heard_at: "2026-09-25T09:30:00Z",
      heard_node_id: null,
      heard_name: null,
      heard_how: "unknown",
      relay_byte: 0x2b,
      rx_snr: 2,
      rx_rssi: -105,
      margin: 9.5,
      packet_row_id: null,
    },
  ],
};

describe("Coverage here", () => {
  it("shows SNR for each radio setting surveyed in the cell", () => {
    render(
      <CoverageCellDetails
        detail={{ data: cell, error: null, loading: false }}
        onlyOf={null}
        isDrawn={() => true}
        onChooseNode={() => {}}
        onClose={() => {}}
      />,
    );

    const values = screen
      .getAllByRole("definition")
      .map((value) => value.textContent ?? "");
    const sf7 = values.find((value) => /\bSF7\b/.test(value));
    const sf11 = values.find((value) => /\bSF11\b/.test(value));
    expect(sf7).toMatch(/6\.8\s*dB average/);
    expect(sf11).toMatch(/-12\.0\s*dB average/);
  });

  it("names the radio settings of a cell surveyed on one", () => {
    render(
      <CoverageCellDetails
        detail={{
          data: { ...cell, networks: cell.networks.slice(1) },
          error: null,
          loading: false,
        }}
        onlyOf={null}
        isDrawn={() => true}
        onChooseNode={() => {}}
        onClose={() => {}}
      />,
    );

    const values = screen
      .getAllByRole("definition")
      .map((value) => value.textContent ?? "");
    expect(values.some((value) => /\bSF11\b.*250 kHz/.test(value))).toBe(true);
  });

  it("lists heard nodes and lets a drawn one be chosen", () => {
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

    expect(
      screen.getByText("Guess: linked to a gateway that heard the same copy"),
    ).toBeTruthy();
    // Its channel follows how it was heard.
    expect(screen.getByText("Heard directly · LongFast")).toBeTruthy();
    const heard = screen.getByRole("list", { name: "Nodes heard here" });
    expect(within(heard).getByText("Unknown relay ending in 2b")).toBeTruthy();
    expect(
      within(heard).getByText("Maybe Ridge (3.2 km), VLY (12 km)"),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Hilltop" }));
    expect(choose).toHaveBeenCalledWith(0x0100001a);
    // A node the map does not draw links to its page instead.
    expect(
      screen.getByRole("link", { name: "Far away" }).getAttribute("href"),
    ).toBe("#nodes/7");
  });

  it("names each latest measurement by the node heard, not the sender", () => {
    render(
      <CoverageCellDetails
        detail={{ data: cell, error: null, loading: false }}
        onlyOf={null}
        isDrawn={() => true}
        onChooseNode={() => {}}
        onClose={() => {}}
      />,
    );

    const rows = within(
      screen.getByRole("list", { name: "Latest measurements" }),
    )
      .getAllByRole("listitem")
      .map((row) => row.textContent ?? "");
    expect(rows[0]).toMatch(/Hilltop \(guess\)$/);
    expect(rows[1]).toMatch(/Unknown relay ending in 2b$/);
  });
});
