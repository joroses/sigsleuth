import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MapNode } from "./api";
import { LinkQualityLegend, MapLinkDetails } from "./MapLinkDetails";
import { DirectionSnr } from "./ui";

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
    from_map_report: false,
    precision_km: null,
  },
});

const link = {
  node_a: 1,
  node_b: 2,
  last_seen_at: "2026-09-25T09:00:00Z",
  channel: "LongFast",
  spreading_factor: 11,
  a_to_b: {
    observations: 2,
    snr_samples: 2,
    average_snr: 4,
    average_rssi: null,
  },
  b_to_a: {
    observations: 0,
    snr_samples: 0,
    average_snr: null,
    average_rssi: null,
  },
};

describe("A selected traceroute link", () => {
  it("keeps measured SNR when the radio channel is unknown", () => {
    render(<DirectionSnr direction={link.a_to_b} spreadingFactor={null} />);
    expect(screen.getByText("4.0")).toBeDefined();
    expect(screen.queryByTitle(/margin/)).toBeNull();
  });
  it("lists each direction's hop cost and packets under it, linking to the hop", async () => {
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
                free: null,
              },
            ],
            b_to_a: [],
            // Valencia is a client, so it spends a hop on Madrid's copies.
            a_to_b_cost: {
              free: 0,
              costly: 0,
              verdict: "never_free",
              role_node_id: 2,
              role: "CLIENT_MUTE",
            },
            b_to_a_cost: {
              free: 5,
              costly: 1,
              verdict: "free",
              role_node_id: null,
              role: null,
            },
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
    expect(
      screen.getByRole("link", { name: "Madrid" }).getAttribute("href"),
    ).toBe("#nodes/1");
    // The card: each way's SNR, tier, times heard and hop cost (Madrid
    // first; no short names, so the ids' last digits), then the distance.
    const card = screen.getByRole("region", { name: "Link summary" });
    expect(
      [...card.querySelectorAll(".link-card-way")].map(
        (way) => way.textContent,
      ),
    ).toEqual([
      "0001 → 00024.0 dBGood2 observationsCosts a hop",
      "0002 → 0001—Not heard0 observations0-hop5 of 6",
    ]);
    expect(within(card).getByText("Costs a hop").getAttribute("title")).toMatch(
      /^Costs a hop: Valencia is Client mute\./,
    );
    expect(card.querySelector(".link-card-channel")!.textContent).toBe(
      "LongFast",
    );
    expect(card.textContent).toContain("Last seen");
    // Between the two nodes: no distance, so no line of sight either.
    const madrid = screen.getByRole("link", { name: "Madrid" });
    const valencia = screen.getByRole("link", { name: "Valencia" });
    const unknown = screen.getByText("Distance unknown");
    expect(madrid.textContent).toBe("Mad…Madrid!00000001");
    expect(before(madrid, unknown) && before(unknown, valencia)).toBe(true);
    expect(before(valencia, card)).toBe(true);
    expect(screen.queryByRole("link", { name: /Line of sight/ })).toBe(null);
    // Each way's packets under the card, in the same order.
    const hears = screen.getByText("Madrid → Valencia");
    const heard = screen.getByText("Valencia → Madrid");
    const none = screen.getByText("None in this window");
    expect(before(card, hears) && before(hears, entry)).toBe(true);
    expect(before(entry, heard) && before(heard, none)).toBe(true);
    expect(
      screen
        .getByRole("link", { name: /All traceroutes/ })
        .getAttribute("href"),
    ).toBe("#links/traceroutes/1-2");
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
