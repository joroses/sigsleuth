import type { ContactLink, LinkKind, LinkRow, NodeContact } from "./api";
import { fromNode } from "./linkRows";
import { type LinkFeatureCollection, linkHalves } from "./mapLinkLayers";
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
  /** Contacts with no position, not drawn. */
  unplaced: number;
}

/**
 * The contacts of the `shown` groups placed on the node's minimap. With
 * gateways shown, a contact with a direct or neighbour link takes the direct
 * tone; else a gateway takes the tone of the fewest hops it heard the node's
 * packets after, as on a packet page, labelled with that number; a
 * traceroute partner alone is muted. Lines are the node's `links` (its Links
 * table's rows) of the shown groups, from `origin`, the node's current
 * position, to placed contacts; none without an origin.
 */
export function contactLayers(
  nodeId: number,
  contacts: NodeContact[],
  links: LinkRow[],
  origin: Place | null,
  shown: ContactGroups,
): ContactLayers {
  const marks: ContactMark[] = [];
  const places = new Map<number, Place>();
  let unplaced = 0;
  for (const contact of contacts) {
    const radio = contact.links.some((link) => link.kind !== "traceroute");
    const gateway = shown.gateways && (radio || contact.heard_packets > 0);
    const partner =
      shown.traceroutes &&
      contact.links.some((link) => link.kind === "traceroute");
    if (!gateway && !partner) continue;
    const place = contact.node;
    if (place?.latitude == null || place.longitude == null) {
      unplaced += 1;
      continue;
    }
    marks.push({
      contact,
      latitude: place.latitude,
      longitude: place.longitude,
      estimated: place.estimated,
      tone: !gateway
        ? "hop-tone-relay"
        : radio
          ? "hop-tone-direct"
          : contactHopTone(contact),
      label:
        !gateway || contact.fewest_hops === null
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
  if (origin !== null) {
    for (const row of links) {
      // node_a is the node, so the half at its end shows what it hears.
      const link = fromNode(row, nodeId);
      const place = places.get(link.node_b);
      const group =
        link.link_kind === "traceroute" ? shown.traceroutes : shown.gateways;
      if (place === undefined || !group) continue;
      lines[link.link_kind].features.push(
        ...linkHalves(
          `${link.link_kind}:${link.node_b}:${link.channel}`,
          link,
          [origin.longitude, origin.latitude],
          [place.longitude, place.latitude],
        ),
      );
    }
  }
  return { marks, lines, unplaced };
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
