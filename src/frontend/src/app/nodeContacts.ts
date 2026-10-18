import type { FeatureCollection, LineString } from "geojson";

import type { ContactLink, NodeContact } from "./api";
import { formatKm } from "./nodeDisplay";

/** The node page's contacts cover this many days. */
export const CONTACT_DAYS = 7;

type Place = { latitude: number; longitude: number };

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
  /** From the node to each placed linked contact; radio is a direct or neighbour link. */
  lines: FeatureCollection<LineString, { radio: boolean }>;
  /** Contacts with no position, not drawn. */
  unplaced: number;
}

/**
 * The contacts placed on the node's minimap. A contact with a direct or
 * neighbour link takes the direct tone; else a gateway takes the tone of
 * the fewest hops it heard the node's packets after, as on a packet page,
 * labelled with that number; a traceroute partner alone is muted. Lines
 * start at `origin`, the node's current position, and are left out without one.
 */
export function contactLayers(
  contacts: NodeContact[],
  origin: Place | null,
): ContactLayers {
  const marks: ContactMark[] = [];
  const lines: ContactLayers["lines"] = {
    type: "FeatureCollection",
    features: [],
  };
  let unplaced = 0;
  for (const contact of contacts) {
    const place = contact.node;
    if (place?.latitude == null || place.longitude == null) {
      unplaced += 1;
      continue;
    }
    const radio = contact.links.some((link) => link.kind !== "traceroute");
    marks.push({
      contact,
      latitude: place.latitude,
      longitude: place.longitude,
      estimated: place.estimated,
      tone: radio ? "hop-tone-direct" : contactHopTone(contact),
      label: contact.fewest_hops === null ? "" : String(contact.fewest_hops),
    });
    if (origin !== null && contact.links.length > 0) {
      lines.features.push({
        type: "Feature",
        geometry: {
          type: "LineString",
          coordinates: [
            [origin.longitude, origin.latitude],
            [place.longitude, place.latitude],
          ],
        },
        properties: { radio },
      });
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
