import type {
  ContactLink,
  LinkKind,
  LinkRow,
  NodeContact,
  ZeroCostHop,
} from "./api";
import { fromNode } from "./linkRows";
import {
  type LinkFeatureCollection,
  type ZeroCostArrows,
  linkHalves,
  zeroCostArrows,
} from "./mapLinkLayers";
import { formatKm } from "./nodeDisplay";

/** The node page's contacts cover this many days. */
export const CONTACT_DAYS = 7;

type Place = { latitude: number; longitude: number };

/**
 * The contact groups shown: traceroute partners, and gateways (nodes that
 * heard its packets, or with a direct or neighbour link).
 */
export interface ContactGroups {
  traceroutes: boolean;
  gateways: boolean;
}

/** A placed contact's marker: its tone and label say how close a tie it has. */
export interface ContactMark {
  contact: NodeContact;
  latitude: number;
  longitude: number;
  estimated: boolean;
  tone: string;
  label: string;
}

export interface ContactLayers {
  marks: ContactMark[];
  /**
   * From the node to each placed contact, a line per link (kind and
   * channel), in halves coloured by signal as on the Map.
   */
  lines: Record<LinkKind, LinkFeatureCollection>;
  zeroCost: ZeroCostArrows;
  /** Contacts with no position, not drawn. */
  unplaced: number;
  moved: number;
}

/**
 * A partner's 0-cost hops with the node on one channel: out when the partner
 * relays the node's copies without spending a hop, into when the node
 * relays the partner's.
 */
export interface ZeroCostWays {
  out: ZeroCostHop | null;
  into: ZeroCostHop | null;
}

/** The node's 0-cost hops by zeroCostKey of partner and channel. */
export function zeroCostWays(
  nodeId: number,
  hops: ZeroCostHop[],
): Map<string, ZeroCostWays> {
  const ways = new Map<string, ZeroCostWays>();
  for (const hop of hops) {
    const out = hop.from_node_id === nodeId;
    const key = zeroCostKey(
      out ? hop.to_node_id : hop.from_node_id,
      hop.channel,
    );
    const found = ways.get(key) ?? { out: null, into: null };
    ways.set(key, out ? { ...found, out: hop } : { ...found, into: hop });
  }
  return ways;
}

export function zeroCostKey(partner: number, channel: string): string {
  return `${partner}:${channel}`;
}

/** Which way the node's hop with the partner is free. */
export function zeroCostLabel({ out, into }: ZeroCostWays): string {
  return out !== null && into !== null
    ? "0-hop both ways"
    : out !== null
      ? "0-hop out"
      : "0-hop in";
}

/** How often the hop was free, each way the label names. */
export function zeroCostTitle({ out, into }: ZeroCostWays): string {
  const times = (hop: ZeroCostHop) =>
    `${hop.free.toLocaleString()} of ${(hop.free + hop.costly).toLocaleString()}`;
  if (out !== null && into !== null)
    return `Out ${times(out)} traceroutes, in ${times(into)}`;
  return `${times((out ?? into)!)} traceroutes`;
}

/** Placed contacts of the shown groups, with lines heard since both ends'
 * last move. Markers need a line or a gateway reception since both moves.
 * Arrowheads follow the visible traceroute lines.
 */
export function contactLayers(
  nodeId: number,
  contacts: NodeContact[],
  links: LinkRow[],
  zeroCost: ZeroCostHop[],
  origin: Place | null,
  shown: ContactGroups,
  movedAt: string | null = null,
): ContactLayers {
  const marks: ContactMark[] = [];
  const places = new Map<number, Place>();
  let unplaced = 0;
  let moved = 0;
  const rows = new Map<number, LinkRow[]>();
  for (const row of links) {
    const link = fromNode(row, nodeId);
    const group =
      link.link_kind === "traceroute" ? shown.traceroutes : shown.gateways;
    if (!group) continue;
    const found = rows.get(link.node_b) ?? [];
    found.push(link);
    rows.set(link.node_b, found);
  }
  const trusted: LinkRow[] = [];
  for (const contact of contacts) {
    const candidates = rows.get(contact.node_id) ?? [];
    const kept = candidates.filter(
      (link) =>
        (movedAt === null ||
          Date.parse(link.last_seen_at) >= Date.parse(movedAt)) &&
        (contact.moved_at === null ||
          Date.parse(link.last_seen_at) >= Date.parse(contact.moved_at)),
    );
    trusted.push(...kept);
    const radio = kept.some((link) => link.link_kind !== "traceroute");
    const gateway = shown.gateways && contact.heard_packets > 0;
    if (kept.length === 0 && !gateway) {
      if (
        candidates.length > 0 ||
        (shown.gateways && contact.heard_before_move)
      )
        moved += 1;
      continue;
    }
    const place = contact.node;
    if (place?.latitude == null || place.longitude == null) {
      unplaced += 1;
      continue;
    }
    if (origin === null && !gateway) continue;
    marks.push({
      contact,
      latitude: place.latitude,
      longitude: place.longitude,
      estimated: place.estimated,
      tone:
        !gateway && !radio
          ? "hop-tone-relay"
          : radio
            ? "hop-tone-direct"
            : contactHopTone(contact),
      label:
        (!gateway && !radio) || contact.fewest_hops === null
          ? ""
          : String(contact.fewest_hops),
    });
    places.set(contact.node_id, {
      latitude: place.latitude,
      longitude: place.longitude,
    });
  }
  const empty = (): LinkFeatureCollection => ({
    type: "FeatureCollection",
    features: [],
  });
  const lines = { traceroute: empty(), direct: empty(), neighbor: empty() };
  const traceroutes: LinkRow[] = [];
  if (origin !== null) {
    places.set(nodeId, origin);
    for (const link of trusted) {
      const place = places.get(link.node_b);
      if (place === undefined) continue;
      const halves = linkHalves(
        `${link.link_kind}:${link.node_b}:${link.channel}`,
        link,
        [origin.longitude, origin.latitude],
        [place.longitude, place.latitude],
      );
      lines[link.link_kind].features.push(...halves);
      if (link.link_kind === "traceroute") traceroutes.push(link);
    }
  }
  return {
    marks,
    lines,
    zeroCost: zeroCostArrows(traceroutes, zeroCost, places),
    unplaced,
    moved,
  };
}

function contactHopTone(contact: NodeContact): string {
  if (contact.fewest_hops !== null) {
    return `hop-tone-${Math.min(contact.fewest_hops, 3)}`;
  }
  return contact.heard_packets > 0 ? "hop-tone-unknown" : "hop-tone-relay";
}

const LINK_NAMES: Record<ContactLink["kind"], string> = {
  direct: "Heard directly",
  neighbor: "Neighbour report",
  traceroute: "Traceroute hop",
};

/**
 * What ties a contact to the node, a line each: every link kind with its
 * observations and its distances when heard (a wide range means an end's
 * position changed), and how many of the node's packets it heard.
 */
export function contactFacts(contact: NodeContact): string[] {
  const facts = contact.links.map((link) => {
    const count = `${link.observations.toLocaleString()}×`;
    const km =
      link.min_km === null || link.max_km === null
        ? "never both placed"
        : link.max_km - link.min_km < 1
          ? `${formatKm(link.min_km)} km when heard`
          : `${formatKm(link.min_km)}–${formatKm(link.max_km)} km when heard`;
    return `${LINK_NAMES[link.kind]} ${count}, ${km}`;
  });
  if (contact.heard_packets > 0) {
    const hops =
      contact.fewest_hops === null
        ? "hops not reported"
        : contact.fewest_hops === 0
          ? "with no hops used"
          : `after ${contact.fewest_hops} ${contact.fewest_hops === 1 ? "hop" : "hops"} at fewest`;
    facts.push(
      `Gateway for ${contact.heard_packets.toLocaleString()} of its packets, ${hops}`,
    );
  }
  return facts;
}
