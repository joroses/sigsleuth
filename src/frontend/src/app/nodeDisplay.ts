import type { LinkKind } from "./api";

export function formatNodeId(value: number): string {
  return `!${value.toString(16).padStart(8, "0")}`;
}

export const BROADCAST_NODE_ID = 0xffffffff;

export function nodeHref(nodeId: number): string {
  return `#nodes/${nodeId}`;
}

/** Opens the map with the node selected, even when its filters would hide it. */
export function mapHref(nodeId: number): string {
  return `#map/${nodeId}`;
}

/** The links with the node searched. */
export function nodeLinksHref(nodeId: number): string {
  return `#links/${nodeId}`;
}

/** The traceroute explorer's runs through the node. */
export function nodeTraceroutesHref(nodeId: number): string {
  return `#links/traceroutes/${nodeId}`;
}

/** The Links page with this link's row expanded. */
export function linkDetailsHref(
  linkKind: LinkKind,
  nodeA: number,
  nodeB: number,
): string {
  return `#links/${nodeA}-${nodeB}?kind=${linkKind}`;
}

/**
 * The terrain profile between two nodes; with `at`, a time, where they were
 * then by their own positions.
 */
export function lineOfSightHref(
  nodeA: number,
  nodeB: number,
  at?: string,
): string {
  const time = at === undefined ? "" : `@${Date.parse(at)}`;
  return `#links/line-of-sight/${nodeA}-${nodeB}${time}`;
}

/** The traceroute explorer's runs with a hop between two nodes. */
export function tracerouteHopHref(nodeA: number, nodeB: number): string {
  return `#links/traceroutes/${nodeA}-${nodeB}`;
}

/**
 * Diagnostics' packets that sender sent, only those heardDirectlyBy heard
 * with no relay when it is given.
 */
export function diagnosticsPacketsHref(
  sender: number,
  heardDirectlyBy: number | null = null,
): string {
  return heardDirectlyBy === null
    ? `#diagnostics/packets/${sender}`
    : `#diagnostics/packets/${sender}-${heardDirectlyBy}`;
}

/** A packet's page showing its relays. */
export function packetRelaysHref(packetRowId: number): string {
  return `#packets/${packetRowId}/relays`;
}

/** A packet's page showing only the reports where from_node was heard by to_node. */
export function packetHopHref(
  packetRowId: number,
  fromNodeId: number,
  toNodeId: number,
): string {
  return `#packets/${packetRowId}/hop/${fromNodeId}-${toNodeId}`;
}

/** A node's long name, else its short name; null when it has reported neither. */
export function nodeName(
  node: { long_name: string | null; short_name: string | null } | undefined,
): string | null {
  return node?.long_name ?? node?.short_name ?? null;
}

export function nodeLabel(node: {
  node_id: number;
  long_name: string | null;
  short_name: string | null;
}): string {
  return nodeName(node) ?? formatNodeId(node.node_id);
}

/** The names of the nodes among nodeIds that doubtful holds, once each, as
 * DoubtfulBadge's suspects. */
export function doubtedNames(
  nodeIds: readonly (number | null)[],
  doubtful: ReadonlySet<number>,
  nodes: ReadonlyMap<
    number,
    { long_name: string | null; short_name: string | null }
  >,
): string[] {
  return [...new Set(nodeIds)]
    .filter((nodeId) => nodeId !== null && doubtful.has(nodeId))
    .map((nodeId) => nodeName(nodes.get(nodeId!)) ?? formatNodeId(nodeId!));
}

export function nodeMarkerLabel(node: {
  node_id: number;
  long_name: string | null;
  short_name: string | null;
}): string {
  const short = node.short_name?.trim();
  if (short) {
    return [...short].slice(0, 4).join("");
  }
  const long = node.long_name?.trim();
  if (long) {
    const chars = [...long];
    return chars.length <= 4 ? long : `${chars.slice(0, 3).join("")}…`;
  }
  return formatNodeId(node.node_id).slice(-4);
}

// Device roles read as words: CLIENT_MUTE -> "Client mute".
export function formatRole(role: string | null): string {
  if (role === null) return "Not reported";
  const unknown = /^UNKNOWN_(\d+)$/.exec(role);
  if (unknown) return `Unknown (${unknown[1]})`;
  const words = role.toLowerCase().replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Hardware models keep their product spelling: HELTEC_V3 -> "HELTEC V3".
export function formatHardware(model: string | null): string {
  if (model === null) return "Not reported";
  const unknown = /^UNKNOWN_(\d+)$/.exec(model);
  if (unknown) return `Unknown (${unknown[1]})`;
  return model.replaceAll("_", " ");
}

// The heading a packet page groups a reception under, matching the
// order the API lists them in.
export function hopGroup(reception: {
  hop_start: number | null;
  hop_limit: number | null;
  direct: boolean;
  own_upload?: boolean;
}): string {
  if (reception.own_upload) return "Uploaded by the sender itself, not heard";
  if (reception.direct) return "Heard directly";
  if (reception.hop_start === null || reception.hop_limit === null) {
    return "Hops not reported";
  }
  const hops = reception.hop_start - reception.hop_limit;
  if (hops === 0) return "No hops used, not confirmed direct";
  return hops === 1 ? "1 hop" : `${hops} hops`;
}

// Hops used by a reception, for the minimap marker: "0" when direct or no hops
// were used, "?" when the packet did not say.
export function hopsUsed(reception: {
  hop_start: number | null;
  hop_limit: number | null;
  direct: boolean;
}): string {
  if (reception.direct) return "0";
  if (reception.hop_start === null || reception.hop_limit === null) return "?";
  return String(reception.hop_start - reception.hop_limit);
}

// The colour class shared by a hop group heading and its minimap markers.
export function hopTone(reception: {
  hop_start: number | null;
  hop_limit: number | null;
  direct: boolean;
  own_upload?: boolean;
}): string {
  if (reception.own_upload) return "hop-tone-unknown";
  const hops = hopsUsed(reception);
  if (reception.direct) return "hop-tone-direct";
  if (hops === "?") return "hop-tone-unknown";
  return `hop-tone-${Math.min(Number(hops), 3)}`;
}

// The last byte of the node that relayed the packet to this gateway. On a
// direct reception the byte is the sender's own, so it says nothing new; 0
// means the firmware did not fill it in.
export function relayByte(reception: {
  relay_byte: number | null;
  direct: boolean;
}): string {
  if (reception.direct || !reception.relay_byte) return "—";
  return `0x${reception.relay_byte.toString(16).padStart(2, "0")}`;
}

export function relayByteText(relayByte: number): string {
  return relayByte.toString(16).padStart(2, "0");
}

/** The plain words for a relay guessed from its byte. */
export function relayGuessText(
  how: "reported" | "gateway_link" | "sender_link" | "only_candidate",
  byte: string,
): string {
  switch (how) {
    case "reported":
      return "Guess: it relayed the packet (MQTT), maybe not the copy heard here";
    case "gateway_link":
      return "Guess: linked to a gateway that heard the same copy";
    case "sender_link":
      return "Guess: linked to the sender";
    case "only_candidate":
      return `Guess: the only node ending in ${byte} that could have relayed it`;
  }
}

/** Why a reception's relay byte names its node, for its tooltip. */
export function relayNodeTitle(relay: {
  node_id: number;
  candidates: number;
  how:
    | "heard_directly"
    | "reported"
    | "gateway_link"
    | "sender_link"
    | "only_candidate";
}): string {
  const byte = relayByteText(relay.node_id & 0xff);
  if (relay.how === "heard_directly")
    return `Heard directly: the only node ending in ${byte} this gateway heard directly within a week of the packet`;
  const text = relayGuessText(relay.how, byte);
  return relay.candidates > 1
    ? `${text}. ${relay.candidates} nodes ending in ${byte} could have relayed it.`
    : text;
}

/**
 * The tooltip of a relay byte that names no node, listing the maybes. A
 * gateway sending its own byte uploaded its own rebroadcast, so the byte
 * names it, not the node it heard.
 */
export function relayMaybeTitle(reception: {
  gateway_node_id: number;
  relay_byte: number | null;
  relay_maybe: { node_id: number; name: string | null; km: number | null }[];
}): string {
  // Firmware sends FF for a number ending in 00, as 0 means no relay.
  if (reception.relay_byte === (reception.gateway_node_id & 0xff || 0xff))
    return "This gateway's own byte: it uploaded its own rebroadcast, which says nothing about the node it heard";
  const maybe = reception.relay_maybe
    .map(
      (node) =>
        `${node.name ?? shortLabel(node.node_id, null)}${node.km === null ? "" : ` (${node.km} km)`}`,
    )
    .join(", ");
  const byte = relayByteText(reception.relay_byte ?? 0);
  return `Unknown relay ending in ${byte}${maybe === "" ? "" : `. Maybe ${maybe}`}`;
}

// A node's short name for compact lists, else the last four hex digits of its ID.
export function shortLabel(nodeId: number, shortName: string | null): string {
  return shortName?.trim() || formatNodeId(nodeId).slice(-4);
}

/** The long name, falling back to the short label when there is none. */
export function longLabel(
  nodeId: number,
  longName: string | null,
  shortName: string | null,
): string {
  return longName?.trim() || shortLabel(nodeId, shortName);
}

/** A distance in km: one decimal under 10 km, whole km above; "—" when unknown. */
export function formatKm(km: number | null): string {
  if (km === null) return "—";
  return km < 10 ? km.toFixed(1) : Math.round(km).toLocaleString();
}

/**
 * How a node with no reported position was placed and how rough it is, such
 * as "No reported position. Estimated from 3 radio neighbours, about 5 km away."
 */
export function estimateText(estimate: {
  neighbours: number;
  spread_km: number;
  from_traceroute: boolean;
}): string {
  const kind = estimate.from_traceroute
    ? "traceroute neighbour"
    : "radio neighbour";
  const plural = estimate.neighbours === 1 ? "" : "s";
  const near =
    estimate.neighbours === 1
      ? "placed at it"
      : `about ${Math.max(1, Math.round(estimate.spread_km))} km away`;
  const rough = estimate.from_traceroute
    ? " Rough: traceroute hops can be long."
    : "";
  return `No reported position. Estimated from ${estimate.neighbours} ${kind}${plural}, ${near}.${rough}`;
}

/**
 * Where a position from a node's map report comes from and how rough it is,
 * such as "From its map report, rounded on purpose: within about 1.5 km."
 * precisionKm is null when the report gave it exactly.
 */
export function mapReportPositionText(precisionKm: number | null): string {
  if (precisionKm === null) return "From its map report.";
  return `From its map report, rounded on purpose: within about ${formatKm(precisionKm)} km.`;
}

/** A reported firmware string without its build hash: "2.7.15.567b8ea" -> "2.7.15". */
export function shortFirmware(version: string): string {
  return /^\d+\.\d+\.\d+/.exec(version)?.[0] ?? version;
}
