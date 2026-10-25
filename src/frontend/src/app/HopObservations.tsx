import { type HopCost, type NodeSummary, type HopObservations } from "./api";
import {
  formatNodeId,
  formatRole,
  nodeName,
  packetHopHref,
} from "./nodeDisplay";
import { type ApiState } from "./useApi";
import { Badge, HopSnr, ShortTime } from "./ui";

/**
 * What a link direction's hop cost says, with a hover title for some;
 * name names the node whose role decides a never_free direction.
 */
function hopCostText(
  cost: HopCost,
  name: (nodeId: number) => string,
): { text: string; title?: string } {
  const total = (cost.free + cost.costly).toLocaleString();
  switch (cost.verdict) {
    case "free":
      return {
        text: `0-hop: free in ${cost.free.toLocaleString()} of ${total} traceroutes`,
        title: "Passed on without spending a hop, as for a favourite router",
      };
    case "costly":
      return {
        text: `Costs a hop: ${cost.costly.toLocaleString()} of ${total} traceroutes`,
      };
    case "never_free":
      return {
        text: `Costs a hop: ${name(cost.role_node_id!)} is ${formatRole(cost.role)}`,
        title:
          "Only a router passes another router's copies on without spending a hop",
      };
    case "unknown":
      return {
        text: "Unknown",
        title: "No traceroute showed whether it spends a hop",
      };
  }
}

/**
 * One direction's hop cost, from the same request as its packets; nothing
 * until it loads. unknown replaces "Unknown" where no label says what is.
 */
export function HopCostLine({
  hops,
  direction,
  name,
  unknown,
}: {
  hops: ApiState<HopObservations>;
  direction: "a_to_b" | "b_to_a";
  name: (nodeId: number) => string;
  unknown?: string;
}) {
  const cost = hops.data?.[`${direction}_cost`] ?? null;
  if (cost === null) return null;
  const { text, title } = hopCostText(cost, name);
  return (
    <span title={title}>
      {cost.verdict === "unknown" && unknown !== undefined ? unknown : text}
    </span>
  );
}

/**
 * The newest traceroute packets that reported one direction of a link, from
 * one request for both directions. Each opens its packet page showing
 * only the reports with this hop. Loading, an empty window and an error each
 * show as one muted line, so the rest of the page stays. Compact, for the
 * narrow map sidebar, is a collapsed list of just the time and SNR.
 */
export function HopObservationList({
  hops,
  direction,
  fromNodeId,
  toNodeId,
  spreadingFactor,
  compact = false,
}: {
  hops: ApiState<HopObservations>;
  direction: "a_to_b" | "b_to_a";
  /** The node transmitting in this direction, and the node hearing it. */
  fromNodeId: number;
  toNodeId: number;
  /** Rates each SNR's tier; without it the SNRs have no dot. */
  spreadingFactor?: number;
  compact?: boolean;
}) {
  if (hops.error !== null) return <p className="muted">{hops.error}</p>;
  if (hops.data === null) return <p className="muted">Loading…</p>;
  const items = hops.data[direction];
  if (items.length === 0) {
    return <p className="muted">None in this window</p>;
  }
  const nodes = new Map(hops.data.nodes.map((node) => [node.node_id, node]));
  const list = (
    <ul className="hop-observations" aria-label="Packets with this hop">
      {items.map((item) => (
        <li key={item.packet_row_id}>
          <a
            className="text-button"
            href={packetHopHref(item.packet_row_id, fromNodeId, toNodeId)}
          >
            <ShortTime value={item.first_received_at} />
          </a>
          <HopSnr
            className="number"
            snr={item.snr_db}
            spreadingFactor={spreadingFactor}
            digits={1}
          />
          {item.free === true ? (
            <Badge tone="blue" title="This traceroute showed it free">
              0-hop
            </Badge>
          ) : item.free === false ? (
            <span className="muted" title="This traceroute spent a hop on it">
              1 hop
            </span>
          ) : null}
          {compact ? null : (
            <>
              <span className="hop-observation-run">
                {name(
                  nodes.get(item.requester_node_id),
                  item.requester_node_id,
                )}{" "}
                → {name(nodes.get(item.target_node_id), item.target_node_id)}
              </span>
              {item.is_reply ? <span className="muted">reply</span> : null}
            </>
          )}
        </li>
      ))}
    </ul>
  );
  if (!compact) return list;
  return (
    <details className="hop-observations-toggle">
      <summary>Latest {items.length}</summary>
      {list}
    </details>
  );
}

function name(node: NodeSummary | undefined, nodeId: number): string {
  return nodeName(node) ?? formatNodeId(nodeId);
}
