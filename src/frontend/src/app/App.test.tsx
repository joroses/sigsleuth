import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { KeyRelay, MapLink } from "./api";
import { ApplicationBar, DIAGNOSTICS_LINKED } from "./ui";
import { loadSetting } from "./remember";
import { forgetKept } from "./useApi";

const packet = {
  packet_row_id: 7,
  first_received_at: "2026-09-19T09:00:00Z",
  last_received_at: "2026-09-19T09:00:02Z",
  reception_count: 3,
};

const reception = {
  reception_id: 21,
  received_at: "2026-09-19T09:00:02Z",
  gateway_node_id: 0xbbbbbbbb,
  gateway_name: "Bob",
  gateway_short_name: "BOB",
  gateway_latitude: 51.5,
  gateway_longitude: -0.1,
  gateway_estimated: false,
  rx_snr: 6.5,
  rx_rssi: -97,
  hop_start: 3,
  hop_limit: 3,
  relay_byte: 0x11,
  paths: null,
  hops: [],
  reached: false,
  direct: true,
  relay_node: null,
  relay_maybe: [],
  km: null,
};

afterEach(() => {
  cleanup();
  window.location.hash = "";
  window.history.replaceState(null, "");
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  forgetKept();
  Reflect.deleteProperty(navigator, "clipboard");
});

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  return writeText;
}

/** How long before now an ISO time in `url`'s `name` parameter is, in hours. */
function hoursBefore(url: string, name: string): number {
  const at = new URL(url, "http://localhost").searchParams.get(name);
  return (Date.now() - Date.parse(at ?? "")) / 3_600_000;
}

describe("Packet detail", () => {
  it("reports an API error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: "database_unavailable",
              message: "Packet history is temporarily unavailable.",
            },
          },
          503,
        ),
      ),
    );

    window.location.hash = "#packets/7";
    render(<App />);

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Packet history is temporarily unavailable.",
    );
  });

  it("pins, navigates and shares reports on a hop from the hash", async () => {
    const writeText = stubClipboard();
    const [requester, relay, otherRelay, target] = [
      0x11111111, 0x33333333, 0x44444444, 0x22222222,
    ];
    const placed = (node_id: number, name: string) => ({
      node_id,
      long_name: name,
      short_name: null,
      role: null,
      latitude: 40,
      longitude: node_id / 0x11111111,
    });
    const routed = (reception_id: number, via: number) => ({
      ...reception,
      reception_id,
      gateway_node_id: 0xbbbbbb00 + reception_id,
      paths: {
        forward: { nodes: [requester, via, target], snr_db: [1, 2] },
        return_path: null,
      },
      hops: [
        { from_node_id: requester, to_node_id: via, snr_db: 1 },
        { from_node_id: via, to_node_id: target, snr_db: 2 },
      ],
    });
    const fetchMock = vi.fn<(url: string) => Promise<Response>>(() =>
      Promise.resolve(
        jsonResponse({
          packet: {
            ...packet,
            channel: "LongFast",
            sender_node_id: requester,
            recipient_node_id: target,
            packet_id: 1234,
            portnum: "TRACEROUTE_APP",
          },
          hops: [],
          route_nodes: [
            placed(requester, "Home"),
            placed(relay, "Hilltop"),
            placed(otherRelay, "Valley"),
            placed(target, "Coast"),
          ],
          relay_nodes: [],
          spreading_factor: 11,
          traceroute: {
            requester_node_id: requester,
            target_node_id: target,
          },
          reply_route: null,
          hop_costs: [],
          receptions: [routed(21, relay), routed(22, otherRelay)],
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = `#packets/7/hop/${requester}-${relay}`;
    const { container } = render(<App />);

    const listed = () => [
      ...new Set(
        [...container.querySelectorAll("tr[data-reception-id]")].map(
          (row) => (row as HTMLElement).dataset.receptionId,
        ),
      ),
    ];
    // Only the report with the hop is listed, both its nodes ringed.
    await waitFor(() => expect(listed()).toEqual(["21"]));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/packets/7");
    expect(
      [...container.querySelectorAll(".route-node.hop-end")].map(
        (node) => node.textContent,
      ),
    ).toEqual([
      expect.stringContaining("Home"),
      expect.stringContaining("Hilltop"),
    ]);

    // The map's share link keeps the hop.
    const mapShare = within(
      container.querySelector(".packet-minimap")!,
    ).getByRole("button", { name: "Share a link to this view" });
    fireEvent.click(mapShare);
    await waitFor(() =>
      expect(writeText).toHaveBeenLastCalledWith(
        new URL(`#packets/7/hop/${requester}-${relay}`, window.location.href)
          .href,
      ),
    );
    writeText.mockClear();

    // Pinning a report keeps the page on the hop.
    fireEvent.click(container.querySelector("tr[data-reception-id='21']")!);
    expect(listed()).toEqual(["21"]);
    const firstShare = within(
      container.querySelector("tr[data-reception-id='21']")!,
    ).getByRole("button", {
      name: "Share a link to this view",
    });
    fireEvent.click(firstShare);
    await waitFor(() =>
      expect(writeText).toHaveBeenLastCalledWith(
        new URL(`${window.location.hash}?reception=21`, window.location.href)
          .href,
      ),
    );
    expect(
      container.querySelector("tr.pinned")?.getAttribute("data-reception-id"),
    ).toBe("21");
    fireEvent.click(mapShare);
    await waitFor(() =>
      expect(writeText).toHaveBeenLastCalledWith(
        new URL(`${window.location.hash}?reception=21`, window.location.href)
          .href,
      ),
    );
    fireEvent.click(firstShare);
    expect(
      container.querySelector("tr.pinned")?.getAttribute("data-reception-id"),
    ).toBe("21");
    fireEvent.click(container.querySelector("tr[data-reception-id='21']")!);
    expect(container.querySelector("tr.pinned")).toBeNull();

    const pinnedId = () =>
      container.querySelector("tr.pinned")?.getAttribute("data-reception-id");
    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    expect(pinnedId()).toBe("21");
    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    expect(pinnedId()).toBe("21");

    // The rest follow below, the hop's report staying first.
    fireEvent.click(
      screen.getByRole("button", { name: "Show 1 report without the hop" }),
    );
    expect(listed()).toEqual(["21", "22"]);
    fireEvent.keyDown(document.body, { key: "ArrowDown", ctrlKey: true });
    expect(pinnedId()).toBe("21");
    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    expect(pinnedId()).toBe("22");
    fireEvent.keyDown(document.body, { key: "ArrowDown" });
    expect(pinnedId()).toBe("22");
    fireEvent.keyDown(document.body, { key: "ArrowUp" });
    expect(pinnedId()).toBe("21");
    fireEvent.keyDown(document.body, { key: "ArrowUp" });
    expect(pinnedId()).toBe("21");
    fireEvent.click(container.querySelector("tr[data-reception-id='21']")!);
    fireEvent.keyDown(document.body, { key: "ArrowUp" });
    expect(pinnedId()).toBe("22");
    fireEvent.click(
      within(container.querySelector("tr[data-reception-id='22']")!).getByRole(
        "button",
        { name: "Share a link to this view" },
      ),
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(4));
    const sharedUrl = writeText.mock.calls.at(-1)![0] as string;
    cleanup();
    window.history.replaceState(null, "", sharedUrl);
    const reopened = render(<App />);
    await waitFor(() =>
      expect(
        reopened.container
          .querySelector("tr.pinned")
          ?.getAttribute("data-reception-id"),
      ).toBe("22"),
    );
    expect(
      reopened.container.querySelector("tr[data-reception-id='21']"),
    ).not.toBeNull();
  });

  it("times reports from the first one, even when it is hidden", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce(
        jsonResponse({
          packet: {
            ...packet,
            channel: "LongFast",
            sender_node_id: 0x11111111,
            recipient_node_id: 0x22222222,
            packet_id: 1234,
            portnum: "TRACEROUTE_APP",
          },
          hops: [],
          route_nodes: [],
          relay_nodes: [],
          spreading_factor: 11,
          traceroute: {
            requester_node_id: 0x11111111,
            target_node_id: 0x22222222,
          },
          reply_route: null,
          hop_costs: [],
          receptions: [
            {
              ...reception,
              received_at: "2026-09-19T09:00:00Z",
              paths: {
                forward: { nodes: [0x11111111, 0x33333333], snr_db: [4] },
                return_path: null,
              },
            },
            {
              ...reception,
              reception_id: 22,
              gateway_node_id: 0xcccccccc,
              gateway_name: "Carol",
              received_at: "2026-09-19T09:00:20Z",
              paths: {
                forward: {
                  nodes: [0x11111111, 0x33333333, 0x44444444],
                  snr_db: [4, 2],
                },
                return_path: null,
              },
            },
          ],
        }),
      ),
    );

    window.location.hash = "#packets/7";
    render(<App />);

    await screen.findByRole("heading", { name: "Packet 1234" });
    const rows = screen.getAllByRole("row").map((row) => row.textContent ?? "");
    // Bob's earlier report is hidden beneath Carol's longer route.
    expect(rows.some((row) => row.includes("Bob"))).toBe(false);
    expect(rows.find((row) => row.includes("Carol"))).toContain("+20.0 s");
  });

  // A broadcast two gateways reported, one through a relay.
  const relayedPacket = {
    packet: {
      ...packet,
      channel: "LongFast",
      sender_node_id: 0x11111111,
      recipient_node_id: 0xffffffff,
      packet_id: 1234,
      portnum: null,
    },
    hops: [],
    route_nodes: [],
    relay_nodes: [],
    spreading_factor: 11,
    traceroute: null,
    reply_route: null,
    hop_costs: [],
    receptions: [
      reception,
      {
        ...reception,
        reception_id: 22,
        gateway_node_id: 0xcccccccc,
        hop_limit: 2,
        direct: false,
      },
    ],
  };

  it("opens a packet's relay map from the hash and loads its routes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(relayedPacket))
      .mockResolvedValueOnce(jsonResponse({ hops: [], nodes: [] }));
    vi.stubGlobal("fetch", fetchMock);
    // The link shows the relays whatever was chosen before.
    localStorage.setItem("sigsleuth.packet.map.relays", "false");

    window.location.hash = "#packets/7/relays";
    render(<App />);

    await screen.findByRole("heading", { name: "Packet 1234" });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/packets/7");
    await waitFor(() =>
      expect(fetchMock.mock.calls[1]?.[0]).toBe(
        "/api/v1/packets/7/flood-routes",
      ),
    );
    expect(
      screen
        .getByRole("button", { name: "Relays" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("opens and shares a packet's map with its switches and size", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url === "/api/v1/packets/7" ? relayedPacket : { hops: [], nodes: [] },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const writeText = stubClipboard();
    localStorage.setItem("sigsleuth.packet.map.gateways", "true");

    window.location.hash = "#packets/7?map=expanded&show=relays";
    const { container } = render(<App />);

    const close = await screen.findByRole("button", { name: /Close/ });
    expect(close.getAttribute("aria-pressed")).toBe("true");
    const relays = screen.getByRole("button", { name: "Relays" });
    const gateways = screen.getByRole("button", { name: "Gateways" });
    expect(relays.getAttribute("aria-pressed")).toBe("true");
    expect(gateways.getAttribute("aria-pressed")).toBe("false");
    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
        "/api/v1/packets/7/flood-routes",
      ),
    );
    // The link sets this page only; the saved choices stay.
    expect(loadSetting("packet.map.gateways")).toBe(true);
    expect(loadSetting("packet.map.relays")).toBeUndefined();
    // The map has the share button, not the heading.
    expect(container.querySelector(".packet-heading button")).toBeNull();

    fireEvent.click(gateways);
    fireEvent.click(
      screen.getByRole("button", { name: "Share a link to this view" }),
    );
    await waitFor(() =>
      expect(writeText).toHaveBeenLastCalledWith(
        new URL(
          "#packets/7?map=expanded&show=relays,gateways",
          window.location.href,
        ).href,
      ),
    );
  });

  it("shows a packet's map on a known relay when nothing else is placed", async () => {
    const unplaced = {
      ...reception,
      gateway_latitude: null,
      gateway_longitude: null,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          packet: {
            ...packet,
            channel: "LongFast",
            sender_node_id: 0x11111111,
            recipient_node_id: 0xffffffff,
            packet_id: 1234,
            portnum: null,
          },
          hops: [],
          route_nodes: [],
          relay_nodes: [
            {
              node_id: 42,
              long_name: "Known relay",
              short_name: "KR",
              latitude: 41,
              longitude: 2,
              estimated: false,
            },
          ],
          sender_latitude: null,
          sender_longitude: null,
          spreading_factor: 11,
          traceroute: null,
          reply_route: null,
          hop_costs: [],
          receptions: [
            unplaced,
            {
              ...unplaced,
              reception_id: 22,
              gateway_node_id: 0xcccccccc,
              relay_node: {
                node_id: 42,
                name: "Known relay",
                candidates: 1,
                how: "heard_directly",
              },
              hop_limit: 2,
              direct: false,
            },
          ],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ hops: [], nodes: [] }));
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#packets/7";
    render(<App />);

    await screen.findByRole("heading", { name: "Packet 1234" });
    // The relays are the map, so they show and load their routes.
    const relays = screen.getByRole("button", { name: "Relays" });
    expect(relays.getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByText(/No map/)).toBeNull();
    await waitFor(() =>
      expect(fetchMock.mock.calls[1]?.[0]).toBe(
        "/api/v1/packets/7/flood-routes",
      ),
    );
  });

  it("groups receptions by hops, as the API orders them", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce(
        jsonResponse({
          packet: {
            ...packet,
            channel: "LongFast",
            sender_node_id: 0x11111111,
            recipient_node_id: 0xffffffff,
            packet_id: 1234,
            portnum: null,
          },
          hops: [],
          route_nodes: [],
          relay_nodes: [],
          spreading_factor: 11,
          traceroute: null,
          reply_route: null,
          hop_costs: [],
          receptions: [
            reception,
            {
              ...reception,
              reception_id: 22,
              gateway_node_id: 0xcccccccc,
              gateway_name: "Carol",
              received_at: "2026-09-19T09:00:02.300Z",
              hop_limit: 1,
              relay_byte: 0x3c,
              direct: false,
              relay_node: {
                node_id: 0x1234563c,
                name: "Hilltop",
                candidates: 4,
                how: "gateway_link",
              },
            },
            {
              ...reception,
              reception_id: 24,
              gateway_node_id: 0xeeeeeeee,
              gateway_name: "Erin",
              received_at: "2026-09-19T09:00:02.400Z",
              hop_limit: 1,
              relay_byte: 0x5a,
              direct: false,
              relay_node: {
                node_id: 0x1234565a,
                name: "Ridge",
                candidates: 3,
                how: "heard_directly",
              },
            },
            {
              ...reception,
              reception_id: 23,
              gateway_node_id: 0xdddddddd,
              gateway_name: null,
              rx_snr: null,
              rx_rssi: null,
              hop_start: null,
              hop_limit: null,
              relay_byte: null,
              direct: false,
              relay_node: null,
              relay_maybe: [],
              km: null,
            },
          ],
        }),
      ),
    );

    window.location.hash = "#packets/7";
    render(<App />);

    await screen.findByRole("heading", { name: "Packet 1234" });
    const rows = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.textContent ?? "");
    // Each group heads its receptions: Bob is direct, Carol came over two
    // hops, and the last reported none.
    expect(
      rows.map((row) => row.match(/Bob|Carol|Erin|!dddddddd/)?.[0] ?? "group"),
    ).toEqual(["group", "Bob", "group", "Carol", "Erin", "group", "!dddddddd"]);
    expect(rows[2]).toMatch(/\b2\b/);
    // Bob's relay byte is the sender's own, so none is shown; Carol's names a
    // relay, guessed as Hilltop; Erin heard Ridge directly, so no guess mark.
    expect(rows[1]).not.toContain("0x11");
    expect(rows[3]).toContain("Hilltop");
    expect(rows[3]).toContain("≈");
    expect(rows[4]).toContain("Ridge");
    expect(rows[4]).not.toContain("≈");
  });
});

describe("Node detail", () => {
  const textPacket = {
    ...packet,
    channel: "LongFast",
    packet_id: 1234,
    portnum: "TEXT_MESSAGE_APP",
  };

  it("filters the node's packets by port", async () => {
    const sentText = {
      ...textPacket,
      sender_node_id: 0x11111111,
      sender_name: null,
      recipient_node_id: 0xffffffff,
      recipient_name: null,
    };
    const overview = {
      node: null,
      ports: [
        { portnum: 3, port: "POSITION_APP" },
        { portnum: 1, port: "TEXT_MESSAGE_APP" },
      ],
    };
    const pageWith = (packets: object[]) => ({ packets, next_before_id: null });
    const positionPacket = {
      ...sentText,
      packet_row_id: 9,
      packet_id: 5678,
      portnum: "POSITION_APP",
    };
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url === "/api/v1/nodes/286331153"
            ? overview
            : url.endsWith("?port=1")
              ? pageWith([sentText])
              : pageWith([positionPacket, sentText]),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#nodes/286331153";
    render(<App />);

    expect(await screen.findByText("5678")).not.toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "Port" }), {
      target: { value: "1" },
    });

    await waitFor(() => expect(screen.queryByText("5678")).toBeNull());
    expect(screen.getByText("1234")).not.toBeNull();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/nodes/286331153/packets?port=1",
      expect.anything(),
    );
  });

  it("opens a shared node page's minimap without keeping its switches, and shares it back", async () => {
    const node = {
      node_id: 0x11111111,
      long_name: "Hilltop",
      short_name: "HILL",
      role: null,
      hardware_model: null,
      last_sent_at: null,
      last_sent_packet_row_id: null,
      last_seen_at: "2026-09-19T09:00:00Z",
      channel: "LongFast",
      last_gateway_at: null,
      node_info_observed_at: null,
      node_info_packet_row_id: null,
      position: null,
      location_source: null,
      estimate: null,
      firmware: null,
      map_report: null,
      contact_url: null,
    };
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url === "/api/v1/nodes/286331153"
            ? { node, ports: [] }
            : url.startsWith("/api/v1/nodes/286331153/charts")
              ? { node_id: node.node_id, hours: [], utilization: [] }
              : { items: [], packets: [], next_before_id: null },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const writeText = stubClipboard();
    localStorage.setItem("sigsleuth.node.contacts.gateways", "true");
    const shared = "#nodes/!11111111?map=expanded&show=coverage";
    window.location.hash = shared;
    render(<App />);

    const close = await screen.findByRole("button", { name: /Close/ });
    expect(close.getAttribute("aria-pressed")).toBe("true");
    const requested = () => fetchMock.mock.calls.map(([url]) => url);
    await vi.waitFor(() =>
      expect(requested()).toContain("/api/v1/coverage/extent?node=286331153"),
    );
    expect(requested().some((url) => url.includes("/contacts"))).toBe(false);
    expect(loadSetting("node.contacts.gateways")).toBe(true);
    expect(loadSetting("node.coverage")).toBeUndefined();

    fireEvent.click(
      screen.getByRole("button", { name: "Share a link to this view" }),
    );
    await vi.waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        new URL(shared, window.location.href).href,
      ),
    );
  });

  it("searches a node's links by their other end, ten at a time", async () => {
    const direction = { observations: 3, snr_samples: 0, average_snr: null };
    // Twelve direct links, each to a node above this one, the last named.
    const others = Array.from({ length: 12 }, (_, index) => 0x20000000 + index);
    const table = {
      items: others.map((other) => ({
        node_a: 0x11111111,
        node_b: other,
        last_seen_at: "2026-09-19T09:00:00Z",
        channel: "LongFast",
        spreading_factor: 11,
        a_to_b: direction,
        b_to_a: direction,
        link_kind: "direct",
        distance_km: null,
        doubts: [],
      })),
      nodes: [
        { node_id: others[11], long_name: "Mill Gate", short_name: "MILL" },
      ],
      doubtful_node_ids: [],
      truncated: false,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.startsWith("/api/v1/nodes/286331153/links")
              ? table
              : url.includes("/packets")
                ? { packets: [], next_before_id: null }
                : { node: null, ports: [] },
          ),
        ),
      ),
    );

    window.location.hash = "#nodes/286331153";
    render(<App />);

    expect(await screen.findByText("Showing 10 of 12.")).not.toBeNull();
    fireEvent.change(
      screen.getByRole("searchbox", { name: "Search links by node" }),
      { target: { value: "mill" } },
    );

    expect(await screen.findByText("Showing 1 of 1.")).not.toBeNull();
    expect(screen.getByRole("link", { name: "Mill Gate" })).not.toBeNull();
  });

  it("tags a node's traceroute link 0-cost its free way, only on the hop's partner and channel", async () => {
    const direction = { observations: 3, snr_samples: 0, average_snr: null };
    const row = (other: number, kind: string, channel: string) => ({
      node_a: 0x11111111,
      node_b: other,
      last_seen_at: "2026-09-19T09:00:00Z",
      channel,
      spreading_factor: 11,
      a_to_b: direction,
      b_to_a: direction,
      link_kind: kind,
      distance_km: null,
      doubts: [],
    });
    const table = {
      items: [
        row(0x22222222, "traceroute", "LongFast"),
        row(0x22222222, "traceroute", "MediumFast"),
        row(0x22222222, "direct", "LongFast"),
        row(0x33333333, "traceroute", "LongFast"),
      ],
      nodes: [],
      doubtful_node_ids: [],
      truncated: false,
      // The node relays 0x22222222's copies for free on LongFast, and
      // 0x33333333 relays the node's.
      zero_cost_hops: [
        {
          from_node_id: 0x11111111,
          to_node_id: 0x33333333,
          channel: "LongFast",
          free: 1,
          costly: 0,
        },
        {
          from_node_id: 0x22222222,
          to_node_id: 0x11111111,
          channel: "LongFast",
          free: 4,
          costly: 0,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.startsWith("/api/v1/nodes/286331153/links")
              ? table
              : url.includes("/packets")
                ? { packets: [], next_before_id: null }
                : { node: null, ports: [] },
          ),
        ),
      ),
    );

    window.location.hash = "#nodes/286331153";
    render(<App />);

    const [tag] = await screen.findAllByText("0-hop in");
    expect(screen.getAllByText("0-hop in")).toHaveLength(1);
    const cells = tag.closest("tr")!.querySelectorAll("td");
    expect(cells[1].textContent).toBe("!22222222");
    expect(tag.closest("tr")!.textContent).toContain("LongFast");
    expect(tag.getAttribute("title")).toBe("4 of 4 traceroutes");
    const [out] = screen.getAllByText("0-hop out");
    expect(out.closest("tr")!.querySelectorAll("td")[1].textContent).toBe(
      "!33333333",
    );
  });

  it("still lists packets for a node with nothing current known", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.endsWith("/packets")
              ? {
                  packets: [
                    {
                      ...textPacket,
                      sender_node_id: 0x22222222,
                      sender_name: null,
                      recipient_node_id: 0x11111111,
                      recipient_name: null,
                    },
                  ],
                  next_before_id: null,
                }
              : { node: null, ports: [] },
          ),
        ),
      ),
    );

    window.location.hash = "#nodes/286331153";
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "!11111111" }),
    ).not.toBeNull();
    expect(screen.getByRole("link", { name: "!22222222" })).not.toBeNull();
  });
});

describe("Nodes", () => {
  const node = {
    node_id: 0x11111111,
    long_name: "Hilltop Relay",
    short_name: "HILL",
    role: "ROUTER",
    hardware_model: "RAK4631",
    channel: "LongFast",
    last_sent_at: "2026-09-19T09:00:00Z",
    last_sent_packet_row_id: 7,
    last_gateway_at: "2026-09-19T09:00:00Z",
    position_observed_at: "2026-09-19T08:00:00Z",
    traceroute_hops: 12,
    traceroute_neighbours: 3,
    packets: 40,
    broadcast_texts: 0,
    utilization_observed_at: null,
    channel_utilization: null,
    air_util_tx: null,
    firmware: {
      minimum: "2.6.8",
      reason: "its NodeInfo says whether it accepts messages",
    },
  };

  function stubNodes(items: unknown[], total = items.length) {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        url.startsWith("/api/v1/nodes/channels")
          ? jsonResponse({
              channels: [
                { channel: "LongFast", nodes: 1 },
                { channel: "MediumFast", nodes: 1 },
              ],
            })
          : jsonResponse({ items, total }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("asks for the first page of a chosen channel", async () => {
    const fetchMock = stubNodes([node], 45);

    window.location.hash = "#nodes";
    render(<App />);
    await screen.findByRole("link", { name: "!11111111" });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("21–21 of 45");
    await screen.findByRole("option", { name: "MediumFast (1)" });
    fireEvent.change(screen.getByLabelText("Channel"), {
      target: { value: "MediumFast" },
    });

    const urls = fetchMock.mock.calls.map((call) => call[0]);
    expect(urls).toContain("/api/v1/nodes?sort=traceroute&limit=20&offset=20");
    expect(urls.at(-1)).toBe(
      "/api/v1/nodes?sort=traceroute&limit=20&offset=0&channel=MediumFast",
    );
    localStorage.clear();
  });

  it("turns the firmware sort round on a second click", async () => {
    const fetchMock = stubNodes([node]);

    window.location.hash = "#nodes";
    render(<App />);
    await screen.findByRole("link", { name: "!11111111" });
    const firmware = screen.getByRole("button", { name: /Firmware/ });
    fireEvent.click(firmware);
    fireEvent.click(firmware);

    const urls = fetchMock.mock.calls.map((call) => call[0]);
    expect(urls).toContain("/api/v1/nodes?sort=firmware&limit=20&offset=0");
    expect(urls.at(-1)).toBe(
      "/api/v1/nodes?sort=firmware_oldest&limit=20&offset=0",
    );
    localStorage.clear();
  });
});

describe("Chat", () => {
  it("links an unreadable parent to its packet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce(
        jsonResponse({
          messages: [
            chatMessage({
              packet_row_id: 5,
              text: "pong",
              reply_id: 98,
              reply_to: {
                packet_row_id: 3,
                sender_node_id: 0x11111111,
                sender_short_name: "HILL",
                text: null,
              },
            }),
            chatMessage({ packet_row_id: 6, text: "lost", reply_id: 99 }),
          ],
          next_before_id: null,
          has_newer: false,
        }),
      ),
    );

    window.location.hash = "#chat";
    render(<App />);

    const unreadable = await screen.findByRole("link", {
      name: /unreadable message/,
    });
    expect(unreadable.getAttribute("href")).toBe("#packets/3");
  });

  it("adds new messages from polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        jsonResponse(
          url === "/api/v1/chat/channels"
            ? { channels: [{ channel: "LongFast", messages: 2 }] }
            : {
                messages: url.includes("after_id=1")
                  ? [chatMessage({ packet_row_id: 2, text: "newer" })]
                  : url.includes("after_id")
                    ? []
                    : [chatMessage({ packet_row_id: 1, text: "older" })],
                next_before_id: null,
                has_newer: false,
              },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#chat";
    render(<App />);
    expect(await screen.findByText("older")).not.toBeNull();

    await vi.advanceTimersByTimeAsync(10_000);

    expect(await screen.findByText("newer")).not.toBeNull();
    expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
      "/api/v1/chat?after_id=1",
    );
  });
  it("loads older messages above the loaded ones", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        jsonResponse(
          url === "/api/v1/chat/channels"
            ? { channels: [] }
            : url === "/api/v1/chat?before_id=2"
              ? {
                  messages: [chatMessage({ packet_row_id: 1, text: "older" })],
                  next_before_id: null,
                  has_newer: false,
                }
              : {
                  messages: [chatMessage({ packet_row_id: 2, text: "newest" })],
                  next_before_id: 2,
                  has_newer: false,
                },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#chat";
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));

    expect(await screen.findByText("older")).not.toBeNull();
    expect(screen.getByText("newest")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  });

  it("forgets polled messages when the channel changes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let allLoads = 0;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      let messages: unknown[] = [];
      if (url === "/api/v1/chat") {
        allLoads += 1;
        messages = [
          allLoads === 1
            ? chatMessage({ packet_row_id: 1, text: "first" })
            : chatMessage({ packet_row_id: 3, text: "latest" }),
        ];
      } else if (url === "/api/v1/chat?after_id=1") {
        messages = [chatMessage({ packet_row_id: 2, text: "polled" })];
      }
      return Promise.resolve(
        jsonResponse(
          url === "/api/v1/chat/channels"
            ? { channels: [{ channel: "Quiet", messages: 0 }] }
            : { messages, next_before_id: null, has_newer: false },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#chat";
    render(<App />);
    expect(await screen.findByText("first")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await screen.findByText("polled")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Quiet 0" }));
    await waitFor(() => expect(screen.queryByText("polled")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "All" }));

    expect(await screen.findByText("latest")).not.toBeNull();
    expect(screen.queryByText("polled")).toBeNull();
  });

  it("hides a muted sender, remembers it, and polls with the search", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        jsonResponse(
          url === "/api/v1/chat/channels"
            ? { channels: [] }
            : {
                messages: url.includes("after_id")
                  ? []
                  : [chatMessage({ packet_row_id: 1, text: "meter" })],
                next_before_id: null,
                has_newer: false,
              },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#chat";
    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Mute Hilltop Relay" }),
    );
    fireEvent.change(
      screen.getByRole("searchbox", { name: "Search messages" }),
      {
        target: { value: "café " },
      },
    );
    const searched = `/api/v1/chat?search=caf%C3%A9&hide=${0x11111111}`;
    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => url)).toContain(searched),
    );
    await vi.advanceTimersByTimeAsync(11_000);

    const urls = fetchMock.mock.calls.map(([url]) => url);
    expect(urls).toContain(`/api/v1/chat?hide=${0x11111111}`);
    expect(urls).toContain(`${searched}&after_id=1`);

    // After a reload the sender stays muted until unmuted.
    const reload = async () => {
      cleanup();
      forgetKept();
      fetchMock.mockClear();
      render(<App />);
      await waitFor(() => expect(chatUrls().length).toBeGreaterThan(0));
    };
    const chatUrls = () =>
      fetchMock.mock.calls
        .map(([url]) => url as string)
        .filter(
          (url) => url.startsWith("/api/v1/chat?") || url === "/api/v1/chat",
        );
    await reload();
    expect(chatUrls()[0]).toContain(`hide=${0x11111111}`);
    fireEvent.click(screen.getByText("Muted (1)"));
    fireEvent.click(screen.getByRole("button", { name: "Unmute" }));
    await reload();
    expect(chatUrls()[0]).not.toContain("hide=");
  });
});

describe("Diagnostics", () => {
  const summary = {
    capture: {
      status: "connected",
      last_message_at: null,
      retention_ran_at: null,
      stopped_reason: null,
      skipped: { map_report: 4 },
    },
    api_started_at: "2026-09-19T09:00:00Z",
    api_uptime_seconds: 60,
    database: {
      facts_version: 10,
      database_size_bytes: 4096,
      wal_size_bytes: 0,
      row_counts: { reception: 3 },
      oldest_packet_at: null,
      newest_packet_at: null,
      retention_days: 14,
    },
    skipped: [
      {
        reason: "map_report",
        description: "A map report for public maps.",
        count: 4,
      },
    ],
  };
  const packets = {
    packets: [
      {
        ...packet,
        channel: "LongFast",
        packet_id: 1234,
        portnum: "TEXT_MESSAGE_APP",
        sender_node_id: 0x22222222,
        sender_name: "Valley Base",
        recipient_node_id: 0xffffffff,
        recipient_name: null,
      },
    ],
    next_before_id: 7,
  };

  it("filters packets by port, pages through them and returns to all ports", async () => {
    const row = (
      packet_row_id: number,
      packet_id: number,
      portnum: string,
    ) => ({
      ...packets.packets[0],
      packet_row_id,
      packet_id,
      portnum,
    });
    const ports = [
      { portnum: 3, port: "POSITION_APP" },
      { portnum: 1, port: "TEXT_MESSAGE_APP" },
    ];
    const pages: Record<string, object> = {
      "/api/v1/packets": {
        packets: [
          row(9, 9001, "POSITION_APP"),
          row(8, 8001, "TEXT_MESSAGE_APP"),
        ],
        next_before_id: 8,
      },
      "/api/v1/packets/ports": { ports },
      "/api/v1/diagnostics/longest-links?offset=0&count=30": {
        items: [],
        looked_through: 0,
        lowest_km: null,
        oldest_hour_at: null,
        next_offset: null,
      },
      "/api/v1/diagnostics/doubtful-nodes": { items: [] },
      "/api/v1/packets?port=1": {
        packets: [row(8, 8001, "TEXT_MESSAGE_APP")],
        next_before_id: 8,
      },
      "/api/v1/packets?port=1&before_id=8": {
        packets: [row(5, 5001, "TEXT_MESSAGE_APP")],
        next_before_id: null,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(jsonResponse(pages[url] ?? summary)),
      ),
    );

    window.location.hash = "#diagnostics";
    render(<App />);
    expect(await screen.findByText("9001")).not.toBeNull();

    const port = screen.getByRole("combobox", { name: "Port" });
    fireEvent.change(port, { target: { value: "1" } });
    await waitFor(() => expect(screen.queryByText("9001")).toBeNull());
    expect(screen.getByText("8001")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Load more packets" }));
    expect(await screen.findByText("5001")).not.toBeNull();
    expect(screen.getByText("8001")).not.toBeNull();
    expect(screen.queryByText("9001")).toBeNull();

    fireEvent.change(port, { target: { value: "" } });
    expect(await screen.findByText("9001")).not.toBeNull();
    expect(screen.queryByText("5001")).toBeNull();
  });

  it("lists long links by when last heard far apart", async () => {
    const empty = {
      items: [],
      looked_through: 0,
      lowest_km: null,
      oldest_hour_at: null,
      next_offset: null,
    };
    const pages: Record<string, object> = {
      "/api/v1/packets": packets,
      "/api/v1/packets/ports": { ports: [] },
      "/api/v1/diagnostics/doubtful-nodes": { items: [] },
      "/api/v1/diagnostics/longest-links?offset=0&count=30": empty,
      "/api/v1/diagnostics/longest-links?offset=0&count=30&order=latest": empty,
    };
    const fetch = vi.fn((url: string) =>
      Promise.resolve(jsonResponse(pages[url] ?? summary)),
    );
    vi.stubGlobal("fetch", fetch);

    window.location.hash = "#diagnostics";
    render(<App />);

    fireEvent.change(await screen.findByRole("combobox", { name: "Order" }), {
      target: { value: "latest" },
    });
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/diagnostics/longest-links?offset=0&count=30&order=latest",
        expect.anything(),
      ),
    );
  });

  const diagnosticsTab = () =>
    within(screen.getByRole("navigation", { name: "Main pages" })).queryByRole(
      "link",
      { name: "Diagnostics" },
    );

  it("links Diagnostics in the bar once it has loaded in this browser", async () => {
    render(<ApplicationBar activePage="map" />);
    expect(diagnosticsTab()).toBeNull();
    cleanup();

    const pages: Record<string, object> = {
      "/api/v1/packets": packets,
      "/api/v1/packets/ports": { ports: [] },
      "/api/v1/diagnostics/doubtful-nodes": { items: [] },
      "/api/v1/diagnostics/longest-links?offset=0&count=30": {
        items: [],
        looked_through: 0,
        lowest_km: null,
        oldest_hour_at: null,
        next_offset: null,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(jsonResponse(pages[url] ?? summary)),
      ),
    );
    window.location.hash = "#diagnostics";
    render(<App />);
    await waitFor(() => expect(loadSetting(DIAGNOSTICS_LINKED)).toBe(true));
    cleanup();

    render(<ApplicationBar activePage="map" />);
    expect(diagnosticsTab()?.getAttribute("href")).toBe("#diagnostics");
  });

  it("keeps Diagnostics out of the bar when its password is refused", async () => {
    const refused = {
      error: {
        code: "password_required",
        message: "This needs the admin password.",
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          url.startsWith("/api/v1/diagnostics/")
            ? jsonResponse(refused, 401)
            : jsonResponse({ packets: [], next_before_id: null, ports: [] }),
        ),
      ),
    );
    window.location.hash = "#diagnostics";
    render(<App />);
    expect(
      (await screen.findAllByText(/This needs the admin password/)).length,
    ).toBeGreaterThan(0);
    // Shown while on the page, as any page's own tab is.
    expect(diagnosticsTab()).not.toBeNull();
    cleanup();

    render(<ApplicationBar activePage="map" />);
    expect(diagnosticsTab()).toBeNull();
  });
});

describe("Map text reach", () => {
  const mapNode = (
    node_id: number,
    long_name: string,
    short_name: string,
    role: string | null = null,
  ) => ({
    node_id,
    long_name,
    short_name,
    role,
    hardware_model: null,
    last_sent_at: "2026-09-25T09:00:00Z",
    last_sent_packet_row_id: 7,
    last_seen_at: "2026-09-25T09:00:00Z",
    channel: "LongFast",
    latitude: 40,
    longitude: -3,
    estimate: null,
    position: {
      observed_at: "2026-09-25T09:00:00Z",
      device_time: null,
      latitude: 40,
      longitude: -3,
      altitude_meters: null,
      packet_row_id: 6,
    },
  });
  const sender = (node_id: number, short_name: string, messages: number) => ({
    node_id,
    short_name,
    long_name: `${short_name} long`,
    region: null,
    messages,
    last_sent_at: new Date().toISOString(),
  });

  function mockMap(reach: Record<number, unknown>) {
    const fetchMock = vi.fn((url: string) => {
      if (url.startsWith("/api/v1/map/nodes"))
        return Promise.resolve(
          jsonResponse({
            items: [
              mapNode(1, "Madrid", "MAD"),
              mapNode(2, "Valencia", "VLC", "ROUTER"),
              mapNode(3, "Quiet", "QUI"),
            ],
            truncated: false,
          }),
        );
      if (url.startsWith("/api/v1/map/text-senders"))
        return Promise.resolve(
          jsonResponse({
            items: [
              sender(1, "MAD", 10),
              sender(2, "VLC", 3),
              sender(9, "OFF", 2),
            ],
            truncated: false,
          }),
        );
      if (url === "/api/v1/nodes/9")
        return Promise.resolve(
          jsonResponse({
            node: {
              ...mapNode(9, "OFF long", "OFF"),
              channel: "MediumFast",
              latitude: null,
              longitude: null,
              position: null,
            },
            ports: [],
          }),
        );
      const reachMatch = /\/api\/v1\/map\/text-reach\/(\d+)/.exec(url);
      if (reachMatch !== null)
        return Promise.resolve(jsonResponse(reach[Number(reachMatch[1])]));
      return Promise.resolve(jsonResponse({ items: [], truncated: false }));
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("choosing a chatty node selects it and fetches its text reach", async () => {
    const fetchMock = mockMap({
      1: {
        node_id: 1,
        messages: 10,
        channel: "MediumFast",
        gateways: [
          { node_id: 2, receptions: 9 },
          { node_id: 5, receptions: 4 },
        ],
      },
    });
    window.location.hash = "#map";
    render(<App />);

    const chatty = await screen.findByRole("region", { name: "Chatty nodes" });
    fireEvent.click(await within(chatty).findByRole("button", { name: /MAD/ }));

    expect(
      await screen.findByRole("heading", { name: "Madrid" }),
    ).not.toBeNull();
    await vi.waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          url.startsWith("/api/v1/map/text-reach/1?since="),
        ),
      ).toBe(true),
    );
  });

  it("fetches a searched node's coverage extent on the map's channel only while coverage is on", async () => {
    const fetchMock = mockMap({});
    const extentRequests = () =>
      fetchMock.mock.calls
        .map(([url]) => url)
        .filter((url) => url.startsWith("/api/v1/coverage/extent"));
    const chooseValencia = async () => {
      const box = await screen.findByRole("combobox", { name: "Search nodes" });
      // The box keeps the last query; a click shows its results again.
      fireEvent.click(box);
      fireEvent.change(box, { target: { value: "Valencia" } });
      fireEvent.click(await screen.findByRole("option", { name: /Valencia/ }));
      await screen.findByRole("heading", { name: "Valencia" });
    };
    window.location.hash = "#map";
    render(<App />);

    await chooseValencia();
    expect(extentRequests()).toEqual([]);

    fireEvent.click(screen.getByRole("checkbox", { name: /Coverage/ }));
    await chooseValencia();
    await vi.waitFor(() =>
      expect(extentRequests()).toEqual([
        "/api/v1/coverage/extent?node=2&channel=LongFast",
      ]),
    );
  });

  it("orders a node's traceroute links, opens and steps through them, then returns to the node", async () => {
    const link = (
      node_a: number,
      node_b: number,
      out: number,
      into: number,
      channel = "LongFast",
    ): MapLink => ({
      node_a,
      node_b,
      channel,
      last_seen_at: "2026-09-25T09:00:00Z",
      spreading_factor: 11,
      a_to_b: { observations: out, snr_samples: out, average_snr: 2 },
      b_to_a: { observations: into, snr_samples: into, average_snr: 1 },
    });
    const fetchMock = vi.fn((url: string) => {
      const body = url.startsWith("/api/v1/map/nodes")
        ? {
            items: [
              mapNode(1, "Alpha", "ALPH"),
              mapNode(2, "Origin", "ORIG"),
              mapNode(3, "Beta", "BETA"),
              mapNode(4, "Gamma", "GAMM"),
            ],
            truncated: false,
          }
        : url.startsWith("/api/v1/map/links?")
          ? {
              items: [
                link(2, 3, 10, 12),
                link(1, 2, 40, 30),
                link(2, 4, 100, 0),
                link(1, 3, 500, 500),
                link(2, 5, 80, 80),
                link(2, 3, 300, 300, "MediumFast"),
              ],
              truncated: false,
            }
          : url.startsWith("/api/v1/traceroutes/hops")
            ? { a_to_b: [], b_to_a: [], nodes: [] }
            : url.startsWith("/api/v1/map/text-reach/")
              ? { node_id: 2, messages: 0, channel: null, gateways: [] }
              : { items: [], truncated: false };
      return Promise.resolve(jsonResponse(body));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.location.hash = "#map/2?show=traceroute&channel=LongFast";
    render(<App />);

    const side = await screen.findByRole("complementary", {
      name: "Map sidebar",
    });
    const first = await within(side).findByRole("button", {
      name: "Show link to Alpha",
    });
    const table = first.closest("table")!;
    const rows = within(table).getAllByRole("row").slice(1);
    expect(
      rows.map((row) => within(row).getByRole("button").textContent),
    ).toEqual(["ALPH", "BETA", "GAMM"]);
    expect(within(rows[0]).getAllByRole("cell")[1].textContent).toBe("30⇄40");

    const details = side.querySelector<HTMLElement>(".sidebar-details")!;
    details.scrollTop = 240;
    fireEvent.click(rows[0]);
    expect(details.scrollTop).toBe(0);
    const openHop = () =>
      within(side)
        .getByRole("link", { name: "All traceroutes" })
        .getAttribute("href");
    expect(openHop()).toBe("#links/traceroutes/1-2");
    expect(side.querySelector(".sidebar-step")?.textContent).toBe("1 / 3");

    fireEvent.click(within(side).getByRole("button", { name: "Next link" }));
    expect(openHop()).toBe("#links/traceroutes/2-3");
    expect(side.querySelector(".sidebar-step")?.textContent).toBe("2 / 3");
    fireEvent.click(
      within(side).getByRole("button", { name: "Previous link" }),
    );
    expect(openHop()).toBe("#links/traceroutes/1-2");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => {
          if (!url.startsWith("/api/v1/traceroutes/hops")) return false;
          const query = new URL(url, "http://localhost").searchParams;
          return (
            query.get("node_a") === "1" &&
            query.get("node_b") === "2" &&
            query.get("channel") === "LongFast" &&
            hoursBefore(url, "since") > 71
          );
        }),
      ).toBe(true),
    );

    fireEvent.click(
      within(side).getByRole("button", { name: "Back to Origin" }),
    );
    expect(
      within(side).getByRole("heading", { name: "Origin" }),
    ).not.toBeNull();
    expect(
      within(side).getByRole("button", { name: "Show link to Alpha" }),
    ).not.toBeNull();
    expect(details.scrollTop).toBe(240);
  });

  it("shows a linked node that the saved filters would hide", async () => {
    mockMap({});
    localStorage.setItem(
      "sigsleuth.nodeFilter",
      JSON.stringify({ role: "ROUTER" }),
    );
    window.location.hash = "#map/1";
    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Madrid" }),
    ).not.toBeNull();
  });

  it.each(["traceroute", "direct", "neighbor", null] as const)(
    "opens a shared node's view without keeping its settings, and shares it back (%s)",
    async (layer) => {
      const fetchMock = mockMap({
        2: {
          node_id: 2,
          messages: 3,
          channel: "LongFast",
          gateways: [{ node_id: 1, receptions: 3 }],
        },
      });
      const writeText = stubClipboard();
      const saved = {
        seenWindow: "7d",
        textReach: false,
        coverage: false,
        linkLayers: {
          traceroute: layer !== "traceroute",
          direct: layer !== "direct",
          neighbor: layer !== "neighbor",
        },
        nodeFilter: { role: "ROUTER", channel: "MediumFast" },
      };
      for (const [key, value] of Object.entries(saved))
        localStorage.setItem(`sigsleuth.${key}`, JSON.stringify(value));
      const shared = `#map/!00000002?show=${layer === null ? "" : `${layer},`}reach,coverage&window=24h&channel=LongFast`;
      window.location.hash = shared;
      render(<App />);

      expect(
        await screen.findByRole("heading", { name: "Valencia" }),
      ).not.toBeNull();
      const requested = () => fetchMock.mock.calls.map(([url]) => url);
      await vi.waitFor(() =>
        expect(requested()).toContain(
          "/api/v1/coverage/extent?node=2&channel=LongFast",
        ),
      );
      const reach = requested().find((url) =>
        url.startsWith("/api/v1/map/text-reach/2?"),
      );
      const nodes = requested().find((url) =>
        url.startsWith("/api/v1/map/nodes?"),
      );
      for (const url of [reach, nodes])
        expect(
          hoursBefore(url ?? "", url === reach ? "since" : "seen_since"),
        ).toBeCloseTo(24, 1);
      // The saved role filter would hide Madrid. Its Chatty row waits behind
      // the selected node's details.
      expect(
        screen.getByRole("button", { name: /MAD/, hidden: true }),
      ).not.toBeNull();
      for (const [key, value] of Object.entries(saved))
        expect(loadSetting(key), key).toEqual(value);
      for (const [kind, name] of [
        ["traceroute", "Traceroute links"],
        ["direct", "Heard directly"],
        ["neighbor", "Neighbour info"],
      ])
        expect(
          (
            screen.getByRole("checkbox", {
              name: new RegExp(name),
            }) as HTMLInputElement
          ).checked,
        ).toBe(kind === layer);
      expect([
        ...new Set(
          requested()
            .filter((url) => url.startsWith("/api/v1/map/links?"))
            .map((url) =>
              new URL(url, window.location.href).searchParams.get("link_kind"),
            ),
        ),
      ]).toEqual(layer === null ? [] : [layer]);

      const share = screen.getByRole("button", {
        name: "Share a link to this view",
      });
      await vi.waitFor(() => {
        fireEvent.click(share);
        expect(writeText).toHaveBeenLastCalledWith(
          new URL(shared, window.location.href).href,
        );
      });
    },
  );

  it("selects a chatty node with no position and fetches its text reach", async () => {
    const fetchMock = mockMap({
      9: {
        node_id: 9,
        messages: 2,
        channel: "MediumFast",
        gateways: [{ node_id: 2, receptions: 2 }],
      },
    });
    window.location.hash = "#map";
    render(<App />);

    const chatty = await screen.findByRole("region", { name: "Chatty nodes" });
    fireEvent.click(await within(chatty).findByRole("button", { name: /OFF/ }));

    const card = (
      await screen.findByRole("heading", { name: "OFF long" })
    ).closest("section")!;
    expect(
      fetchMock.mock.calls.some(([url]) =>
        url.startsWith("/api/v1/map/text-reach/9?since="),
      ),
    ).toBe(true);
    expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
      "/api/v1/nodes/9",
    );
    expect(await within(card).findByText("MediumFast")).not.toBeNull();
    expect(within(card).getByText("2")).not.toBeNull();
  });

  it("applies the role filter to the chatty nodes", async () => {
    mockMap({});
    window.location.hash = "#map";
    render(<App />);
    const chatty = await screen.findByRole("region", { name: "Chatty nodes" });
    await within(chatty).findByRole("button", { name: /MAD/ });

    fireEvent.change(screen.getByLabelText("Role"), {
      target: { value: "ROUTER" },
    });

    expect(within(chatty).queryByRole("button", { name: /MAD/ })).toBeNull();
    expect(within(chatty).getByRole("button", { name: /VLC/ })).not.toBeNull();
  });

  it("reloads the chatty nodes when the window changes", async () => {
    const fetchMock = mockMap({});
    window.location.hash = "#map";
    render(<App />);
    await screen.findByRole("button", { name: /MAD/ });

    fireEvent.change(screen.getByLabelText("Last seen within"), {
      target: { value: "all" },
    });

    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
        "/api/v1/map/text-senders",
      ),
    );
  });

  it("shows the nodes and chatty nodes again on Back without loading them", async () => {
    const fetchMock = mockMap({});
    const nodeLoads = () =>
      fetchMock.mock.calls.filter(([url]) =>
        url.startsWith("/api/v1/map/nodes"),
      ).length;
    window.location.hash = "#map";
    render(<App />);
    await screen.findByRole("button", { name: /MAD/ });
    window.location.hash = "#map/1";
    await screen.findByRole("heading", { name: "Madrid" });
    // A new visit minutes after shows the window loaded then.
    expect(nodeLoads()).toBe(1);
    const requests = fetchMock.mock.calls.length;

    window.history.back();
    await vi.waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Madrid" })).toBeNull(),
    );
    expect(await screen.findByRole("button", { name: /MAD/ })).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(requests);
  });
});

describe("Links", () => {
  it("opens a traceroute link's details from its row and links the explorer", async () => {
    const direction = { observations: 3, snr_samples: 3, average_snr: 2 };
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/links/positions")
            ? { items: [] }
            : url.startsWith("/api/v1/links/hours")
              ? {
                  a_to_b: [
                    {
                      hour_start: "2026-09-25T09:00:00Z",
                      observations: 3,
                      average_snr: 2,
                    },
                  ],
                  b_to_a: [],
                }
              : url.startsWith("/api/v1/traceroutes/hops")
                ? {
                    a_to_b: [],
                    b_to_a: [
                      {
                        packet_row_id: 9,
                        first_received_at: "2026-09-25T09:00:00Z",
                        snr_db: 6.25,
                        requester_node_id: 1,
                        target_node_id: 2,
                        is_reply: false,
                      },
                    ],
                    nodes: [],
                  }
                : {
                    items: [
                      {
                        node_a: 1,
                        node_b: 2,
                        last_seen_at: "2026-09-25T09:00:00Z",
                        channel: "LongFast",
                        spreading_factor: 11,
                        a_to_b: direction,
                        b_to_a: direction,
                        link_kind: "traceroute",
                        distance_km: null,
                        doubts: [],
                      },
                    ],
                    nodes: [],
                    truncated: false,
                  },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    window.location.hash = "#links";
    render(<App />);

    // The row links the explorer on the hop, as its opened list does.
    const hopLink = await screen.findByRole("link", {
      name: "All traceroutes with this hop",
    });
    expect(hopLink.getAttribute("href")).toBe("#links/traceroutes/1-2");
    // A click anywhere on the row opens it.
    fireEvent.click(screen.getByText("LongFast"));

    const entry = await screen.findByRole("listitem");
    expect(within(entry).getByRole("link").getAttribute("href")).toBe(
      "#packets/9/hop/2-1",
    );
    expect(
      screen
        .getAllByRole("link", { name: "All traceroutes with this hop" })
        .map((link) => link.getAttribute("href")),
    ).toEqual(["#links/traceroutes/1-2", "#links/traceroutes/1-2"]);
    // Each direction's hours and packets cover the table's window on the
    // row's channel.
    const query = (path: string) =>
      new URL(
        fetchMock.mock.calls
          .map(([url]) => url)
          .find((url) => url.startsWith(path))!,
        "http://test",
      ).searchParams;
    const since = query("/api/v1/links?").get("since");
    expect(query("/api/v1/traceroutes/hops").get("since")).toBe(since);
    const hours = query("/api/v1/links/hours");
    expect(
      ["link_kind", "node_a", "node_b", "channel", "since"].map((name) =>
        hours.get(name),
      ),
    ).toEqual(["traceroute", "1", "2", "LongFast", since]);
    expect(
      screen
        .getAllByRole("img", { name: /^Heard in \d+ of \d+ hours$/ })
        .map((strip) => strip.getAttribute("aria-label")!.split(" ")[2]),
    ).toEqual(["1", "0"]);
    // Neither node has a position, so there is no map to draw.
    expect(await screen.findByText(/has no known position/)).toBeTruthy();
  });

  it("opens on a node's links from the hash, over 7 days and without the kept filters", async () => {
    const direction = { observations: 3, snr_samples: 3, average_snr: 2 };
    const link = (node_a: number, node_b: number) => ({
      node_a,
      node_b,
      last_seen_at: "2026-09-25T09:00:00Z",
      channel: "LongFast",
      spreading_factor: 11,
      a_to_b: direction,
      b_to_a: direction,
      distance_km: null,
      doubts: [],
    });
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/links?")
            ? {
                items: [link(1, 2), link(3, 4)],
                nodes: [
                  { node_id: 1, long_name: "Hilltop", short_name: null },
                  { node_id: 2, long_name: "Ridge", short_name: null },
                  { node_id: 3, long_name: "Farm", short_name: null },
                  { node_id: 4, long_name: "Mill", short_name: null },
                ],
                truncated: false,
              }
            : {},
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    // Choices kept from an earlier visit that would hide the node's links.
    localStorage.setItem("sigsleuth.links.window", JSON.stringify("1h"));
    localStorage.setItem("sigsleuth.links.minObservations", "50");

    window.location.hash = "#links/1";
    render(<App />);

    expect(await screen.findByText("Ridge")).toBeTruthy();
    expect(screen.queryByText("Farm")).toBeNull();
    const since = new URL(
      fetchMock.mock.calls[0][0],
      "http://test",
    ).searchParams.get("since")!;
    expect(
      Math.abs(Date.now() - 7 * 24 * 3_600_000 - Date.parse(since)),
    ).toBeLessThan(60_000);
    // The kept choices come back on the next visit from the menu.
    cleanup();
    forgetKept();
    fetchMock.mockClear();
    window.location.hash = "#links";
    render(<App />);
    expect(
      ((await screen.findByLabelText("Min. observations")) as HTMLInputElement)
        .value,
    ).toBe("50");
    const keptSince = new URL(
      fetchMock.mock.calls
        .map(([url]) => url)
        .find((url) => url.startsWith("/api/v1/links?"))!,
      "http://test",
    ).searchParams.get("since")!;
    expect(
      Math.abs(Date.now() - 3_600_000 - Date.parse(keptSince)),
    ).toBeLessThan(60_000);
  });

  it("shows a node's links again on Back and on a visit soon after without loading them, and afresh later or on Refresh", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const direction = { observations: 3, snr_samples: 3, average_snr: 2 };
    const link = (node_a: number, node_b: number) => ({
      node_a,
      node_b,
      last_seen_at: "2026-09-25T09:00:00Z",
      channel: "LongFast",
      spreading_factor: 11,
      a_to_b: direction,
      b_to_a: direction,
      distance_km: null,
      doubts: [],
    });
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse({
          items: [link(1, 2), link(3, 4)],
          nodes: [
            { node_id: 1, long_name: "Hilltop", short_name: null },
            { node_id: 2, long_name: "Ridge", short_name: null },
            { node_id: 3, long_name: "Farm", short_name: null },
            { node_id: 4, long_name: "Mill", short_name: null },
          ],
          truncated: false,
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#links/1";
    render(<App />);
    expect(await screen.findByText("Ridge")).toBeTruthy();
    // Another node's links, a new visit, open on the same 7-day window.
    window.location.hash = "#links/3";
    expect(await screen.findByText("Mill")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    window.history.back();
    await waitFor(() => expect(screen.queryByText("Mill")).toBeNull());
    expect(screen.getByText("Ridge")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Ten minutes on, a new visit loads afresh.
    vi.setSystemTime(Date.now() + 10 * 60 * 1000);
    window.location.hash = "#links/3";
    expect(await screen.findByText("Mill")).toBeTruthy();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(await screen.findByText("Mill")).toBeTruthy();
  });

  it("ranks links heard both ways above one-way links by observations", async () => {
    const link = (node_a: number, node_b: number, ab: number, ba: number) => ({
      node_a,
      node_b,
      last_seen_at: "2026-09-25T09:00:00Z",
      channel: "LongFast",
      spreading_factor: 11,
      a_to_b: { observations: ab, snr_samples: ab, average_snr: 2 },
      b_to_a: { observations: ba, snr_samples: ba, average_snr: 2 },
      distance_km: null,
      doubts: [],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.startsWith("/api/v1/links?")
              ? {
                  items: [
                    link(1, 2, 0, 5000),
                    link(3, 4, 1200, 2000),
                    link(5, 6, 200, 200),
                    link(7, 8, 10, 0),
                  ],
                  nodes: [1, 3, 5, 7].map((node_id) => ({
                    node_id,
                    long_name: `Node ${node_id}`,
                    short_name: null,
                  })),
                  doubtful_node_ids: [],
                  truncated: false,
                }
              : {},
          ),
        ),
      ),
    );

    window.location.hash = "#links";
    render(<App />);
    await screen.findByText("Node 1");
    fireEvent.click(screen.getByRole("button", { name: /Seen/ }));

    // The weaker side first; one-way links after, busiest first.
    expect(
      screen
        .getAllByRole("row")
        .map((row) => /Node \d/.exec(row.textContent ?? "")?.[0])
        .filter((name) => name !== undefined),
    ).toEqual(["Node 3", "Node 5", "Node 1", "Node 7"]);
  });

  it("ranks flagged links by distance apart from measured ones, and unmeasured last", async () => {
    const direction = { observations: 3, snr_samples: 3, average_snr: 2 };
    const link = (
      node_a: number,
      node_b: number,
      distance_km: number | null,
      doubts: ("doubtful" | "terrain" | "pending")[] = [],
    ) => ({
      node_a,
      node_b,
      last_seen_at: "2026-09-25T09:00:00Z",
      channel: "LongFast",
      spreading_factor: 11,
      a_to_b: direction,
      b_to_a: direction,
      distance_km,
      doubts,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.startsWith("/api/v1/links?")
              ? {
                  items: [
                    link(7, 8, null),
                    link(1, 2, 880, ["doubtful"]),
                    link(5, 6, 600, ["doubtful"]),
                    link(3, 4, 40),
                    link(9, 10, 12),
                    link(11, 12, 300, ["terrain"]),
                    link(13, 14, 500, ["pending"]),
                  ],
                  nodes: [
                    { node_id: 1, long_name: "Hilltop", short_name: null },
                    { node_id: 2, long_name: "Ridge", short_name: null },
                    { node_id: 3, long_name: "Farm", short_name: null },
                    { node_id: 4, long_name: "Mill", short_name: null },
                    { node_id: 5, long_name: "Summit", short_name: null },
                    { node_id: 6, long_name: "Field", short_name: null },
                    { node_id: 7, long_name: "Unplaced", short_name: null },
                    { node_id: 8, long_name: "Barn", short_name: null },
                    { node_id: 9, long_name: "Shed", short_name: null },
                    { node_id: 10, long_name: "Gate", short_name: null },
                    { node_id: 11, long_name: "Tower", short_name: null },
                    { node_id: 12, long_name: "Lake", short_name: null },
                    { node_id: 13, long_name: "Quarry", short_name: null },
                    { node_id: 14, long_name: "Pond", short_name: null },
                  ],
                  doubtful_node_ids: [2, 5],
                  truncated: false,
                }
              : {},
          ),
        ),
      ),
    );

    window.location.hash = "#links";
    render(<App />);

    await screen.findByText("Ridge");
    const order = () =>
      screen
        .getAllByRole("row")
        .map((row) => row.textContent ?? "")
        .map(
          (text) =>
            /Farm|Shed|Ridge|Summit|Quarry|Tower|Unplaced/.exec(text)?.[0],
        )
        .filter((name) => name !== undefined);
    expect(order()).toEqual([
      "Farm",
      "Shed",
      "Ridge",
      "Summit",
      "Quarry",
      "Tower",
      "Unplaced",
    ]);

    // Shortest first puts the flagged links at the top, still longest first;
    // those with no distance stay last.
    fireEvent.click(screen.getByRole("button", { name: /Distance/ }));
    expect(order()).toEqual([
      "Ridge",
      "Summit",
      "Quarry",
      "Tower",
      "Shed",
      "Farm",
      "Unplaced",
    ]);
    // The line of sight opens where the pair was when last heard.
    expect(
      screen
        .getAllByRole("link", { name: "Line of sight" })
        .at(-1)!
        .getAttribute("href"),
    ).toMatch(/^#links\/line-of-sight\/\d+-\d+@\d+$/);

    // A reload keeps shortest first.
    cleanup();
    forgetKept();
    render(<App />);
    await screen.findByText("Ridge");
    expect(order()).toEqual([
      "Ridge",
      "Summit",
      "Quarry",
      "Tower",
      "Shed",
      "Farm",
      "Unplaced",
    ]);
  });
});

// A run from Farm (5) to Mill (6) through Ridge (2) and Hilltop (1).
const FARM_TO_MILL = {
  requester_node_id: 5,
  target_node_id: 6,
  first_received_at: "2026-09-25T09:00:00Z",
  channel: "LongFast",
  spreading_factor: 11,
  request_packet_row_id: 9,
  request_receptions: 1,
  reply_packet_row_id: null,
  reply_receptions: null,
  distance_km: null,
  doubts: [],
  route: {
    packet_row_id: 9,
    is_return: false,
    hops: 3,
    km: null,
    reached: false,
    path: { nodes: [5, 2, 1, 6], snr_db: [3, 4, 5] },
    path_hops: [
      { from_node_id: 5, to_node_id: 2, snr_db: 3 },
      { from_node_id: 2, to_node_id: 1, snr_db: 4 },
      { from_node_id: 1, to_node_id: 6, snr_db: 5 },
    ],
    hop: { from_node_id: 2, to_node_id: 1, snr_db: 4 },
    hop_costs: [],
  },
};

describe("Traceroute explorer", () => {
  it("reopens and shares an expanded run with its searches, then clears the run target when searching", async () => {
    const writeText = stubClipboard();
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/traceroutes/busiest")
            ? {
                requesters: [],
                targets: [],
                relays: [],
                channels: [],
                nodes: [],
              }
            : {
                items: [{ ...FARM_TO_MILL, positions: [] }],
                nodes: [],
                has_more: false,
              },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem("sigsleuth.links.window", JSON.stringify("1h"));
    localStorage.setItem(
      "sigsleuth.traceroutes.filters",
      JSON.stringify({
        sort: "distance",
        channel: "MediumFast",
        minHops: 5,
        successful: true,
      }),
    );
    const hash = "#links/traceroutes?run=9&node=Hilltop&next_to=Ridge";
    window.location.hash = hash;
    render(<App />);

    const viewAll = await screen.findByRole("link", {
      name: "View all routes",
    });
    expect(viewAll.getAttribute("href")).toBe("#packets/9/hop/2-1");
    const query = () =>
      new URL(
        fetchMock.mock.calls
          .map(([url]) => url)
          .filter((url) => url.startsWith("/api/v1/traceroutes?"))
          .at(-1)!,
        "http://test",
      ).searchParams;
    expect([...query()]).toEqual([
      ["sort", "newest"],
      ["limit", "50"],
      ["offset", "0"],
      ["node", "Hilltop"],
      ["next_to", "Ridge"],
      ["packet", "9"],
    ]);
    fireEvent.click(
      screen.getByRole("button", { name: "Share a link to this view" }),
    );
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        new URL(hash, window.location.href).href,
      ),
    );
    expect(
      JSON.parse(localStorage.getItem("sigsleuth.traceroutes.filters")!).sort,
    ).toBe("distance");

    fireEvent.change(screen.getByLabelText("Node on the route"), {
      target: { value: "Farm" },
    });
    await waitFor(() => expect(query().get("node")).toBe("Farm"));
    expect(query().has("packet")).toBe(false);
  });

  it("opens on a node's runs from the hash, over all history and without the kept filters", async () => {
    const kept = {
      sort: "route",
      channel: "MediumFast",
      minHops: 5,
      successful: true,
    };
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/traceroutes/busiest")
            ? {
                requesters: [],
                targets: [],
                relays: [],
                channels: [],
                nodes: [],
              }
            : { items: [], nodes: [], has_more: false },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem("sigsleuth.links.window", JSON.stringify("1h"));
    localStorage.setItem("sigsleuth.traceroutes.filters", JSON.stringify(kept));

    window.location.hash = "#links/traceroutes/1";
    render(<App />);

    await screen.findByText("No traceroute runs match in this window.");
    const runsUrl = fetchMock.mock.calls
      .map(([url]) => url)
      .find((url) => url.startsWith("/api/v1/traceroutes?"))!;
    const query = new URL(runsUrl, "http://test").searchParams;
    expect(query.get("node")).toBe("!00000001");
    expect(query.get("sort")).toBe("route");
    expect(
      ["next_to", "since", "channel", "min_hops", "successful"].filter((name) =>
        query.has(name),
      ),
    ).toEqual([]);
    expect(
      (screen.getByLabelText("Node on the route") as HTMLInputElement).value,
    ).toBe("!00000001");

    // The kept filters and window come back on the next visit from the menu.
    cleanup();
    forgetKept();
    fetchMock.mockClear();
    window.location.hash = "#links/traceroutes";
    render(<App />);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          url.startsWith("/api/v1/traceroutes?"),
        ),
      ).toBe(true),
    );
    const keptQuery = new URL(
      fetchMock.mock.calls
        .map(([url]) => url)
        .find((url) => url.startsWith("/api/v1/traceroutes?"))!,
      "http://test",
    ).searchParams;
    expect(keptQuery.get("channel")).toBe("MediumFast");
    expect(keptQuery.get("min_hops")).toBe("5");
    expect(keptQuery.has("successful")).toBe(true);
    expect(keptQuery.has("node")).toBe(false);
    expect(
      Math.abs(Date.now() - 3_600_000 - Date.parse(keptQuery.get("since")!)),
    ).toBeLessThan(60_000);
  });

  it("fills both node searches from a hop in the hash and marks the searched nodes", async () => {
    const node = (node_id: number, name: string) => ({
      node_id,
      long_name: name,
      short_name: null,
      role: null,
      latitude: null,
      longitude: null,
      estimated: false,
    });
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/traceroutes/busiest")
            ? {
                requesters: [],
                targets: [],
                relays: [],
                channels: [],
                nodes: [],
              }
            : {
                items: [FARM_TO_MILL],
                nodes: [
                  node(1, "Hilltop"),
                  node(2, "Ridge"),
                  node(5, "Farm"),
                  node(6, "Mill"),
                ],
                matched_node_ids: [1, 2],
                has_more: false,
              },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    // Choices kept from an earlier visit that would hide the hop's runs.
    localStorage.setItem("sigsleuth.links.window", JSON.stringify("1h"));
    localStorage.setItem(
      "sigsleuth.traceroutes.filters",
      JSON.stringify({
        sort: "route",
        channel: "MediumFast",
        minHops: 5,
        successful: true,
      }),
    );

    window.location.hash = "#links/traceroutes/1-2";
    render(<App />);

    const route = await screen.findByText("Route with the hop");
    const runsUrl = fetchMock.mock.calls
      .map(([url]) => url)
      .find((url) => url.startsWith("/api/v1/traceroutes?"))!;
    const query = new URL(runsUrl, "http://test").searchParams;
    expect([query.get("node"), query.get("next_to")]).toEqual([
      "!00000001",
      "!00000002",
    ]);
    // Only the sort is kept; the rest start wide.
    expect(query.get("sort")).toBe("route");
    expect(
      ["since", "channel", "min_hops", "successful"].filter((name) =>
        query.has(name),
      ),
    ).toEqual([]);
    expect(
      (screen.getByLabelText("Next to node") as HTMLInputElement).value,
    ).toBe("!00000002");
    const marked = [
      ...route.parentElement!.querySelectorAll(".searched-node a"),
    ].map((link) => link.textContent);
    expect(marked).toEqual(["Ridge", "Hilltop"]);
    // The hop is marked the way this route crossed it, and its packet opens on it.
    expect(
      [...route.parentElement!.querySelectorAll(".hop-end a")].map(
        (link) => link.textContent,
      ),
    ).toEqual(["Ridge", "Hilltop"]);
    expect(
      screen.getByRole("link", { name: "request" }).getAttribute("href"),
    ).toBe("#packets/9/hop/2-1");
  });

  it("searches nodes by name, picks a channel and filters reached routes", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/traceroutes/busiest")
            ? {
                requesters: [],
                targets: [],
                relays: [],
                channels: [{ channel: "MediumFast", count: 12 }],
                nodes: [],
              }
            : { items: [FARM_TO_MILL], nodes: [], has_more: false },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const runsQuery = () =>
      new URL(
        fetchMock.mock.calls
          .map(([url]) => url)
          .filter((url) => url.startsWith("/api/v1/traceroutes?"))
          .at(-1)!,
        "http://test",
      ).searchParams;

    window.location.hash = "#links/traceroutes";
    render(<App />);

    await screen.findByRole("option", { name: "MediumFast (12)" });
    fireEvent.change(screen.getByLabelText("Channel"), {
      target: { value: "MediumFast" },
    });
    fireEvent.change(screen.getByLabelText("Node on the route"), {
      target: { value: "Hilltop" },
    });

    await waitFor(() => expect(runsQuery().get("node")).toBe("Hilltop"));
    expect(runsQuery().get("channel")).toBe("MediumFast");

    fireEvent.click(screen.getByLabelText("Reached only"));
    fireEvent.click(await screen.findByRole("button", { name: /Apart/ }));
    await waitFor(() => expect(runsQuery().get("sort")).toBe("distance"));
    expect(runsQuery().get("successful")).toBe("true");

    // Farthest apart is reached only; unticking that goes back to latest.
    fireEvent.click(screen.getByLabelText("Reached only"));
    await waitFor(() =>
      expect(
        screen
          .getByRole("columnheader", { name: /Heard/ })
          .getAttribute("aria-sort"),
      ).toBe("descending"),
    );
    expect(
      (screen.getByLabelText("Reached only") as HTMLInputElement).checked,
    ).toBe(false);
  });

  it("doubts a route's km only for a node on its measured hops", async () => {
    // Mill (6) is doubtful. The first run did not reach it, so the hop into
    // Mill was not measured and takes no part in its km; the second did.
    const run = (packet_row_id: number, km: number, reached: boolean) => ({
      ...FARM_TO_MILL,
      route: {
        ...FARM_TO_MILL.route,
        packet_row_id,
        km,
        reached,
        path_hops: FARM_TO_MILL.route.path_hops.slice(0, reached ? 3 : 2),
        hop: null,
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.startsWith("/api/v1/traceroutes/busiest")
              ? {
                  requesters: [],
                  targets: [],
                  relays: [],
                  channels: [],
                  nodes: [],
                }
              : {
                  items: [run(9, 120, false), run(10, 340, true)],
                  nodes: [],
                  doubtful_node_ids: [6],
                  has_more: false,
                },
          ),
        ),
      ),
    );

    window.location.hash = "#links/traceroutes";
    render(<App />);

    const badges = await screen.findAllByText("doubtful");
    expect(badges.map((badge) => badge.closest("td")!.textContent)).toEqual([
      expect.stringMatching(/^3 · 340/),
    ]);
  });

  it("lists the open run's hops with the km between where their nodes were heard", async () => {
    // Mill (6) had no position when heard, so its hop has no km; Farm to
    // Ridge is the longest.
    const heard = (node_id: number, longitude: number) => ({
      node_id,
      latitude: 0,
      longitude,
      altitude_meters: null,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.startsWith("/api/v1/traceroutes/busiest")
              ? {
                  requesters: [],
                  targets: [],
                  relays: [],
                  channels: [],
                  nodes: [],
                }
              : {
                  items: [
                    {
                      ...FARM_TO_MILL,
                      positions: [heard(5, 0), heard(2, 1), heard(1, 1.5)],
                    },
                  ],
                  nodes: [],
                  has_more: false,
                },
          ),
        ),
      ),
    );

    window.location.hash = "#links/traceroutes";
    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Show this traceroute's details",
      }),
    );

    const rows = within(document.querySelector(".hop-list tbody")!)
      .getAllByRole("row")
      .map((row) =>
        [...row.querySelectorAll("td")].map((cell) => cell.textContent),
      );
    expect(rows.map((cells) => cells[2])).toEqual(["111", "56", "—"]);
  });

  it("lists what each of the open run's hops spent, and marks a 0-cost one in its route", async () => {
    const learned = (verdict: string, free: number, costly: number) => ({
      free,
      costly,
      verdict,
      role_node_id: verdict === "never_free" ? 6 : null,
      role: verdict === "never_free" ? "CLIENT_MUTE" : null,
    });
    // Farm's first hop always costs; Hilltop was learned to pass Ridge's
    // copies on free; Mill is a client.
    const run = {
      ...FARM_TO_MILL,
      positions: [],
      route: {
        ...FARM_TO_MILL.route,
        hop: null,
        hop_costs: [
          { from_node_id: 5, to_node_id: 2, spent: 1, learned: null },
          {
            from_node_id: 2,
            to_node_id: 1,
            spent: 0,
            learned: learned("free", 3, 1),
          },
          {
            from_node_id: 1,
            to_node_id: 6,
            spent: 1,
            learned: learned("never_free", 0, 0),
          },
        ],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          jsonResponse(
            url.startsWith("/api/v1/traceroutes/busiest")
              ? {
                  requesters: [],
                  targets: [],
                  relays: [],
                  channels: [],
                  nodes: [],
                }
              : { items: [run], nodes: [], has_more: false },
          ),
        ),
      ),
    );

    window.location.hash = "#links/traceroutes";
    render(<App />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Show this traceroute's details",
      }),
    );

    const rows = within(document.querySelector(".hop-list tbody")!)
      .getAllByRole("row")
      .map((row) =>
        [...row.querySelectorAll("td")].map((cell) => cell.textContent),
      );
    expect(rows.map((cells) => cells[3])).toEqual(["1", "≈0", "≈1"]);
    // In the route text, only Ridge to Hilltop is 0-cost.
    const zeros = document.querySelectorAll(".run-route .route-zero-cost");
    expect(zeros).toHaveLength(1);
    expect(zeros[0].closest("li")!.textContent).toContain("!00000001");
  });

  it("searches for a busiest-list node and clears it on a second click", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/traceroutes/busiest")
            ? {
                requesters: [{ node_id: 5, count: 40 }],
                targets: [],
                relays: [],
                channels: [],
                nodes: [
                  {
                    node_id: 5,
                    long_name: "Farm",
                    short_name: null,
                    role: null,
                    latitude: null,
                    longitude: null,
                  },
                ],
              }
            : { items: [], nodes: [], has_more: false },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const runsQuery = () =>
      new URL(
        fetchMock.mock.calls
          .map(([url]) => url)
          .filter((url) => url.startsWith("/api/v1/traceroutes?"))
          .at(-1)!,
        "http://test",
      ).searchParams;

    window.location.hash = "#links/traceroutes";
    render(<App />);

    const row = await screen.findByRole("button", { name: /Farm/ });
    fireEvent.click(row);
    await waitFor(() => expect(runsQuery().get("node")).toBe("!00000005"));
    expect(row.getAttribute("aria-pressed")).toBe("true");
    expect(
      (screen.getByLabelText("Node on the route") as HTMLInputElement).value,
    ).toBe("!00000005");

    // The runs without the node are those this visit loaded first.
    const requests = fetchMock.mock.calls.length;
    fireEvent.click(row);
    await waitFor(() => expect(row.getAttribute("aria-pressed")).toBe("false"));
    expect(
      (screen.getByLabelText("Node on the route") as HTMLInputElement).value,
    ).toBe("");
    expect(fetchMock).toHaveBeenCalledTimes(requests);
  });
});

describe("Line of sight", () => {
  const placed = (node_id: number, long_name: string) => ({
    node_id,
    long_name,
    short_name: long_name.slice(0, 3),
    role: null,
    hardware_model: null,
    last_sent_at: "2026-09-25T09:00:00Z",
    last_sent_packet_row_id: 7,
    last_seen_at: "2026-09-25T09:00:00Z",
    channel: "LongFast",
    last_gateway_at: null,
    latitude: 40 + node_id / 100,
    longitude: -3,
    estimate: null,
    position: {
      observed_at: "2026-09-25T09:00:00Z",
      device_time: null,
      latitude: 40 + node_id / 100,
      longitude: -3,
      altitude_meters: null,
      packet_row_id: 6,
    },
  });

  function mockNodes() {
    const fetchMock = vi.fn((url: string) =>
      url.startsWith("/api/v1/map/nodes")
        ? Promise.resolve(
            jsonResponse({
              items: [placed(1, "Madrid"), placed(2, "Toledo")],
              truncated: false,
            }),
          )
        : // Terrain tiles fail here, as they may in a real browser.
          Promise.reject(new Error("offline")),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("shows the pair from the hash and survives missing terrain", async () => {
    mockNodes();
    window.location.hash = "#links/line-of-sight/1-2";
    render(<App />);

    await screen.findByRole("region", {
      name: "Line of sight summary",
    });
    expect(screen.getByRole("link", { name: "Madrid" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Toledo" })).toBeTruthy();
  });

  it("places the pair where it was at the time in the hash", async () => {
    const fetchMock = mockNodes();
    const at = Date.parse("2026-09-25T09:00:00Z");
    window.location.hash = `#links/line-of-sight/1-2@${at}`;
    render(<App />);

    await screen.findByRole("region", { name: "Line of sight summary" });
    const asked = fetchMock.mock.calls
      .map(([url]) => new URL(url, "http://test"))
      .find((url) => url.pathname === "/api/v1/links/positions");
    expect(Object.fromEntries(asked!.searchParams)).toEqual({
      node_a: "1",
      node_b: "2",
      at: "2026-09-25T09:00:00.000Z",
    });
    // The lookup failed here: the page says it fell back to current positions.
    expect(
      screen
        .getAllByRole("alert")
        .some((alert) => /where they are now/.test(alert.textContent ?? "")),
    ).toBe(true);

    // Swapping keeps the time; picking another node drops it.
    fireEvent.click(screen.getByRole("button", { name: /Swap/ }));
    expect(window.location.hash).toBe(`#links/line-of-sight/2-1@${at}`);
    fireEvent.click(
      await screen.findByRole("button", { name: "Clear node A" }),
    );
    fireEvent.change(await screen.findByLabelText("Node A"), {
      target: { value: "Madrid" },
    });
    fireEvent.click(await screen.findByRole("option", { name: /Madrid/ }));
    expect(window.location.hash).toBe("#links/line-of-sight/1-1");
  });

  it("names the pair in the hash once both ends are picked", async () => {
    mockNodes();
    window.location.hash = "#links/line-of-sight";
    render(<App />);

    fireEvent.change(await screen.findByLabelText("Node A"), {
      target: { value: "Madrid" },
    });
    fireEvent.click(await screen.findByRole("option", { name: /Madrid/ }));
    expect(window.location.hash).toBe("#links/line-of-sight");
    fireEvent.change(screen.getByLabelText("Node B"), {
      target: { value: "Toledo" },
    });
    fireEvent.click(await screen.findByRole("option", { name: /Toledo/ }));
    expect(window.location.hash).toBe("#links/line-of-sight/1-2");

    fireEvent.click(screen.getByRole("button", { name: /Swap/ }));
    expect(window.location.hash).toBe("#links/line-of-sight/2-1");
  });

  it("fetches an end the node list leaves out", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        url.startsWith("/api/v1/map/nodes")
          ? Promise.resolve(
              jsonResponse({ items: [placed(1, "Madrid")], truncated: true }),
            )
          : url === "/api/v1/nodes/2"
            ? Promise.resolve(
                jsonResponse({ node: placed(2, "Toledo"), ports: [] }),
              )
            : Promise.reject(new Error("offline")),
      ),
    );
    window.location.hash = "#links/line-of-sight/1-2";
    render(<App />);

    expect(
      await screen.findByRole("region", { name: "Line of sight summary" }),
    ).toBeTruthy();
    expect(screen.getByRole("link", { name: "Toledo" })).toBeTruthy();
  });

  it("clears a finished pair when going back to the tab without one", async () => {
    mockNodes();
    window.location.hash = "#links/line-of-sight/1-2";
    render(<App />);
    await screen.findByRole("region", { name: "Line of sight summary" });

    window.location.hash = "#links/line-of-sight";
    expect(await screen.findByLabelText("Node A")).toBeTruthy();
    expect(screen.getByLabelText("Node B")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Madrid" })).toBeNull();
  });
});

describe("Routing", () => {
  it("opens the map for the default and removed sample-page hashes", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ items: [], truncated: false })),
    );

    for (const hash of ["", "#dashboard"]) {
      window.location.hash = hash;
      const { container, unmount } = render(<App />);
      expect(container.querySelector(".page-map"), hash).not.toBeNull();
      const current = container.querySelector('a[aria-current="page"]');
      expect(current?.textContent, hash).toContain("Map");
      unmount();
    }
  });

  it("opens zones on the busiest channel and remembers the settings", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(async () => jsonResponse(networkZones()));
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#zones";
    render(<App />);

    expect(await screen.findByRole("region", { name: "Zones" })).not.toBeNull();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/zones?window_hours=72&per_day=2&min_margin_db=4",
    );
    const channel = await screen.findByLabelText<HTMLSelectElement>("Channel");
    await waitFor(() => expect(channel.value).toBe("MediumFast"));

    fireEvent.change(channel, { target: { value: "LongFast" } });

    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
        "/api/v1/zones?window_hours=72&per_day=2&min_margin_db=4&channel=LongFast",
      ),
    );
    // A slider steps through its choices (1, 2, 3, 5 a day) and asks once it
    // rests; two sliders moved together ask once with both.
    fireEvent.change(
      screen.getByLabelText("Observations a day, each direction"),
      { target: { value: "2" } },
    );
    fireEvent.change(
      screen.getByLabelText("SNR margin above the channel floor"),
      { target: { value: "3" } },
    );
    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
        "/api/v1/zones?window_hours=72&per_day=3&min_margin_db=6&channel=LongFast",
      ),
    );
    // Coming back after a reload finds the page as it was left.
    cleanup();
    forgetKept();
    fetchMock.mockClear();
    render(<App />);
    await screen.findByRole("region", { name: "Zones" });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/zones?window_hours=72&per_day=3&min_margin_db=6&channel=LongFast",
    );
    fireEvent.click(await screen.findByRole("button", { name: "Reset" }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
        "/api/v1/zones?window_hours=72&per_day=2&min_margin_db=4&channel=LongFast",
      ),
    );
    expect(screen.queryByRole("button", { name: "Reset" })).toBeNull();
    localStorage.clear();
  });

  it("zones orders text reach, asks hops over its window and keeps its tab", async () => {
    const zonesBody = sampleZones();
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(
          url.startsWith("/api/v1/traceroutes/hops") ||
            url.startsWith("/api/v1/links/hours")
            ? { a_to_b: [], b_to_a: [] }
            : url.startsWith("/api/v1/links/positions")
              ? { items: [] }
              : zonesBody,
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    window.location.hash = "#zones";
    render(<App />);

    // Text reach each way: a figure, a zone with no sender, a zone with no
    // gateway.
    const between = await screen.findByRole("region", {
      name: /Between zones/,
    });
    const [, second, first] = within(between).getAllByRole("row");
    expect(first.textContent).toContain("94%");
    expect(within(first).getByTitle(/^2 → 1/)).not.toBeNull();
    expect(within(second).getByTitle(/^2 → 3/)).not.toBeNull();
    // A pair with no text figure either way stays last in both directions.
    const firstPair = () => within(between).getAllByRole("row")[1];
    fireEvent.click(within(between).getByRole("button", { name: /^Texts/ }));
    expect(firstPair().textContent).toMatch(/^1\./);
    fireEvent.click(within(between).getByRole("button", { name: /^Texts/ }));
    expect(firstPair().textContent).toMatch(/^1\./);

    // A pair's links: solid first by default, then by times seen both ways.
    fireEvent.click(
      within(between).getByRole("button", {
        name: "Show the links between zones 1 and 2",
      }),
    );
    const linkTable = () => between.querySelector(".zones-pair-links")!;
    const firstLink = () =>
      within(linkTable() as HTMLElement).getAllByRole("row")[1].textContent;
    expect(firstLink()).toMatch(/^Ridge/);
    fireEvent.click(
      within(linkTable() as HTMLElement).getByRole("button", { name: /^Seen/ }),
    );
    expect(firstLink()).toMatch(/^Hilltop/);

    // A link's hours and packets are asked over the page's window.
    fireEvent.click(
      within(between).getAllByRole("button", {
        name: "Show this link's details",
      })[0],
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          url.startsWith("/api/v1/traceroutes/hops"),
        ),
      ).toBe(true),
    );
    for (const path of ["/api/v1/traceroutes/hops", "/api/v1/links/hours"]) {
      const url = fetchMock.mock.calls
        .map(([url]) => url)
        .find((url) => url.startsWith(path))!;
      expect(new URL(url, "http://x").searchParams.get("since")).toBe(
        "2026-09-22T09:00:00.000Z",
      );
    }
    localStorage.clear();
  });

  it("zones side panel shows one choice at a time, steps and goes back", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(sampleZones()))),
    );
    window.location.hash = "#zones";
    render(<App />);
    const side = await screen.findByRole("complementary", { name: "Zones" });
    const step = () => side.querySelector(".sidebar-step")?.textContent;
    const back = () =>
      within(side)
        .getByRole("button", { name: /^Back to / })
        .getAttribute("aria-label");

    // A zone takes the list's place; ‹ › and the arrow keys walk the zones.
    fireEvent.click(within(side).getByRole("button", { name: /^1\. Hilltop/ }));
    expect(back()).toBe("Back to Zones");
    expect(step()).toBe("1 / 3");
    fireEvent.click(within(side).getByRole("button", { name: "Next zone" }));
    expect(step()).toBe("2 / 3");
    fireEvent.keyDown(document.body, { key: "ArrowLeft" });
    expect(step()).toBe("1 / 3");

    // Its connection, then a link of it, each one step deeper.
    fireEvent.click(within(side).getByRole("button", { name: /Valley/ }));
    expect(back()).toBe("Back to 1. Hilltop");
    const firstLink = side.querySelector("tbody tr")!;
    expect(firstLink.textContent).toMatch(/^Ridg/);
    fireEvent.click(firstLink);
    expect(back()).toBe("Back to Hilltop ↔ Valley");
    expect(step()).toBe("1 / 2");
    fireEvent.click(within(side).getByRole("button", { name: "Next link" }));
    expect(step()).toBe("2 / 2");

    fireEvent.click(within(side).getByRole("button", { name: /^Back to / }));
    expect(back()).toBe("Back to 1. Hilltop");
    fireEvent.click(within(side).getByRole("button", { name: /^Back to / }));
    expect(back()).toBe("Back to Zones");

    // A key relay replaces the zone it was chosen from, and goes back to it.
    fireEvent.click(within(side).getByRole("button", { name: "Next zone" }));
    fireEvent.click(within(side).getByRole("tab", { name: /Key relays/ }));
    fireEvent.click(within(side).getByRole("button", { name: /^Relay/ }));
    expect(back()).toBe("Back to 2. Valley");
    expect(within(side).getByRole("heading").textContent).toContain("Relay");
    fireEvent.click(within(side).getByRole("button", { name: /^Back to / }));
    fireEvent.click(within(side).getByRole("button", { name: /^Back to / }));
    expect(within(side).queryByRole("button", { name: /^Back to / })).toBe(
      null,
    );

    // The list's tab is remembered.
    fireEvent.click(within(side).getByRole("tab", { name: /Warnings/ }));
    expect(within(side).queryByRole("button", { name: /^Relay/ })).toBeNull();
    cleanup();
    forgetKept();
    render(<App />);
    const again = await screen.findByRole("complementary", { name: "Zones" });
    expect(
      within(again)
        .getByRole("tab", { name: /Warnings/ })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });

  it("zones warnings filters healthy relays and steps through the warnings", async () => {
    const body = sampleZones();
    const healthy = body.key_relays[0];
    body.key_relays = [
      healthy,
      { ...healthy, node_id: 2, battery_level: 10 },
      {
        ...healthy,
        node_id: 5,
        last_seen_at: "2026-09-25T05:00:00Z",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(body))),
    );
    window.location.hash = "#zones";
    render(<App />);
    const side = await screen.findByRole("complementary", { name: "Zones" });
    fireEvent.click(within(side).getByRole("tab", { name: /Warnings/ }));
    const list = within(side).getByRole("list", { name: "Warnings" });
    const rows = within(list).getAllByRole("button");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("Ridge");
    expect(rows[0].textContent).toContain("Battery at 10%");
    expect(rows[1].textContent).toContain("Mill");
    expect(rows[1].textContent).toContain("Last heard 4.0 h ago");

    fireEvent.click(rows[0]);
    expect(side.querySelector(".sidebar-step")?.textContent).toBe("1 / 2");
    expect(within(side).getByRole("heading").textContent).toContain("Ridge");
    fireEvent.click(
      within(side).getByRole("button", { name: "Next key relay" }),
    );
    expect(side.querySelector(".sidebar-step")?.textContent).toBe("2 / 2");
    expect(within(side).getByRole("heading").textContent).toContain("Mill");
    fireEvent.click(
      within(side).getByRole("button", { name: "Back to Warnings" }),
    );
    expect(within(side).getByRole("list", { name: "Warnings" })).not.toBeNull();
  });
});

function chatMessage(fields: Record<string, unknown>) {
  return {
    packet_id: 1,
    received_at: "2026-09-19T09:00:00Z",
    sender_node_id: 0x11111111,
    sender_short_name: "HILL",
    sender_long_name: "Hilltop Relay",
    recipient_node_id: null,
    recipient_short_name: null,
    recipient_long_name: null,
    channel: "LongFast",
    is_reaction: false,
    reply_id: null,
    reply_to: null,
    reception_count: 3,
    farthest_km: null,
    ...fields,
  };
}

/**
 * Three zones: Hilltop and Valley joined by a solid link, Valley and Coast by
 * none, and one key relay in Valley.
 */
function sampleZones() {
  const now = "2026-09-25T09:00:00Z";
  const node = (node_id: number, name: string) => ({
    node_id,
    long_name: name,
    short_name: name.slice(0, 4),
    role: "ROUTER",
    latitude: 40,
    longitude: -3 + node_id / 10,
    estimated: false,
  });
  const zone = (
    zone_id: number,
    name: string,
    members: number[],
    text_gateways = 2,
  ) => ({
    zone_id,
    name,
    place: null,
    region: null,
    hub_node_id: members[0],
    members,
    internal_links: 3,
    text_gateways,
    text_senders: 1,
    text_count: 1,
    latitude: 40,
    longitude: -3,
    solid_paths: [],
    hull: [],
  });
  const pair = (zone_a: number, zone_b: number, fields: object) => ({
    zone_a,
    zone_b,
    observations: 12,
    strongest_margin_db: 8,
    hub_hops: 2,
    km: 10,
    through_nodes_a: 1,
    through_nodes_b: 1,
    solid_links: 0,
    way_round_links: null,
    way_round_zones: [],
    single_link: false,
    hanging_zones: [],
    warnings: [],
    sole_relay: null,
    ...fields,
  });
  const keyRelay = (node_id: number, zone_id: number, fields: object) => ({
    node_id,
    zone_id,
    impact: 0,
    cuts_off: 0,
    no_solid_backup: 0,
    carries: 0,
    only_solid_link_to: [],
    neighbours: 3,
    last_seen_at: now,
    battery_level: null,
    battery_observed_at: null,
    firmware: null,
    ...fields,
  });
  return networkZones({
    zones: [
      zone(1, "Hilltop", [1, 2, 3]),
      zone(2, "Valley", [4, 5, 6]),
      zone(3, "Coast", [7, 8, 9], 0),
    ],
    text_reach: [
      {
        from_zone: 1,
        to_zone: 2,
        senders: 3,
        texts: 40,
        share: 0.94,
      },
    ],
    zone_pairs: [
      // Listed first, so it stays first among equal distances. Its only solid
      // alternative path is through Hilltop's zone.
      pair(2, 3, {
        warnings: ["no solid link"],
        way_round_links: 2,
        way_round_zones: [1],
        links: [
          {
            node_a: 5,
            node_b: 7,
            observations: 12,
            weaker_margin_db: 8,
            km: 20,
            solid: false,
            last_seen_at: "2026-09-25T08:00:00Z",
            spreading_factor: 9,
            a_to_b: { observations: 6, snr_samples: 6, average_snr: 2 },
            b_to_a: { observations: 6, snr_samples: 6, average_snr: 1 },
          },
        ],
      }),
      pair(1, 2, {
        solid_links: 1,
        warnings: ["one solid link"],
        links: [
          {
            node_a: 2,
            node_b: 6,
            observations: 12,
            weaker_margin_db: 8,
            km: 388,
            solid: true,
            last_seen_at: "2026-09-25T08:00:00Z",
            spreading_factor: 9,
            a_to_b: { observations: 6, snr_samples: 6, average_snr: 2 },
            b_to_a: { observations: 6, snr_samples: 6, average_snr: 1 },
          },
          {
            node_a: 1,
            node_b: 5,
            observations: 40,
            weaker_margin_db: 2,
            km: 15,
            solid: false,
            last_seen_at: "2026-09-25T08:00:00Z",
            spreading_factor: 9,
            a_to_b: { observations: 20, snr_samples: 20, average_snr: -4 },
            b_to_a: { observations: 20, snr_samples: 20, average_snr: -5 },
          },
        ],
      }),
    ],
    key_relays: [keyRelay(6, 2, { impact: 3, cuts_off: 3 })],
    nodes: [
      node(1, "Hilltop"),
      node(2, "Ridge"),
      node(5, "Mill"),
      { ...node(6, "Relay"), estimated: true },
      node(7, "Coast"),
    ],
  });
}

function networkZones(fields: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-25T09:00:00Z",
    window_hours: 72,
    channel: "MediumFast",
    per_day: 1,
    min_margin_db: 4,
    days: 3,
    link_count: 40,
    solid_link_count: 12,
    channels: [
      { channel: "MediumFast", links: 40 },
      { channel: "LongFast", links: 3 },
    ],
    truncated: false,
    zones: [],
    zone_pairs: [],
    rules: {
      weak_margin_db: 4,
      long_route_hops: 4,
      key_relay_cut: 2,
    },
    text_reach: [],
    path_links: [],
    text_reach_truncated: false,
    key_relays: [] as KeyRelay[],
    text_hours: [],
    active_channels: [],
    nodes: [],
    ...fields,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
