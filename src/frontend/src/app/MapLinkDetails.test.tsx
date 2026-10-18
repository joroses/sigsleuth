import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MapNode } from "./api";
import { LinkQualityLegend, MapLinkDetails } from "./MapLinkDetails";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const node = (node_id: number, long_name: string): MapNode => ({
  node_id,
  long_name,
  short_name: null,
  role: null,
  hardware_model: null,
  last_sent_at: "2026-09-25T09:00:00Z",
  last_sent_packet_row_id: 1,
  last_gateway_at: null,
  last_seen_at: "2026-09-25T09:00:00Z",
  channel: "LongFast",
  latitude: 40,
  longitude: node_id - 4,
  estimate: null,
  position: {
    observed_at: "2026-09-25T09:00:00Z",
    device_time: null,
    latitude: 40,
    longitude: node_id - 4,
    altitude_meters: null,
    packet_row_id: 1,
    precision_km: null,
  },
});

const link = {
  node_a: 1,
  node_b: 2,
  last_seen_at: "2026-09-25T09:00:00Z",
  channel: "LongFast",
  spreading_factor: 11,
  a_to_b: { observations: 2, snr_samples: 2, average_snr: 4 },
  b_to_a: { observations: 0, snr_samples: 0, average_snr: null },
};

describe("A selected traceroute link", () => {
  it("lists the packets under each direction, collapsed, linking to the hop", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            a_to_b: [
              {
                packet_row_id: 9,
                first_received_at: "2026-09-25T09:00:00Z",
                snr_db: null,
                requester_node_id: 3,
                target_node_id: 2,
                is_reply: true,
              },
            ],
            b_to_a: [],
            nodes: [
              {
                node_id: 3,
                name: "Hilltop",
                short_name: "HILL",
                role: null,
                latitude: null,
                longitude: null,
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MapLinkDetails
        linkKind="traceroute"
        link={link}
        nodeA={node(1, "Madrid")}
        nodeB={node(2, "Valencia")}
        km={null}
        since="2026-09-24T09:00:00.000Z"
        onClose={() => {}}
      />,
    );

    // Valencia hearing Madrid is a_to_b; Madrid hearing Valencia has none.
    const entry = await screen.findByRole("listitem");
    const before = (first: Element, second: Element) =>
      Boolean(
        first.compareDocumentPosition(second) &
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    // The SVG arrow has an accessible label rather than text content.
    const hears = screen.getByText("Madrid Valencia", { selector: "p" });
    const heard = screen.getByText("Valencia Madrid", { selector: "p" });
    expect(within(hears).getByRole("img", { name: "to" })).toBeTruthy();
    expect(within(heard).getByRole("img", { name: "to" })).toBeTruthy();
    const none = screen.getByText("None in this window");
    expect(before(hears, entry) && before(entry, heard)).toBe(true);
    expect(before(heard, none)).toBe(true);
    expect(within(entry).getByRole("link").getAttribute("href")).toBe(
      "#packets/9/hop/1-2",
    );
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      "/api/v1/traceroutes/hops?node_a=1&node_b=2&since=2026-09-24T09%3A00%3A00.000Z&channel=LongFast",
      expect.anything(),
    );
  });
});

describe("The link quality legend", () => {
  it("switches a coloured tier off, leaving links without an SNR unchecked", () => {
    const onChoice = vi.fn();
    render(
      <LinkQualityLegend
        choice={{ good: true, fair: true, marginal: false }}
        onChoice={onChoice}
      />,
    );

    expect(screen.getAllByRole("checkbox")).toHaveLength(3);
    expect(
      screen.getByRole<HTMLInputElement>("checkbox", { name: "Marginal" })
        .checked,
    ).toBe(false);
    screen.getByRole("checkbox", { name: "Fair" }).click();

    expect(onChoice).toHaveBeenCalledWith({
      good: true,
      fair: false,
      marginal: false,
    });
  });
});
