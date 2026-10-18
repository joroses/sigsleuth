import { type NodeSummary, type HopObservations } from "./api";
import { formatNodeId, nodeName, packetHopHref } from "./nodeDisplay";
import { type ApiState } from "./useApi";
import { HopSnr, ShortTime } from "./ui";

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
