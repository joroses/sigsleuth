import type { PacketDetail } from "./api";
import {
  type DirectedHop,
  type TracerouteDrawing,
  routeRows,
  sameHop,
} from "./minimapMarkers";
import {
  formatNodeId,
  nodeLabel,
  packetHopHref,
  shortLabel,
} from "./nodeDisplay";
import { MarkerChip } from "./RouteItem";
import { Badge, HopSnr, Icon, ShortTime } from "./ui";

/**
 * A traceroute drawn on the map, opened from a link's sidebar: the link, the
 * hop of it whose latest traceroutes list the packet, and the packet.
 */
export interface ShownTraceroute {
  linkKey: string;
  hop: DirectedHop;
  packet: number;
}

/**
 * What is hovered in the list or on the map: a node, or a hop, which in the
 * list is the row of the node it reached.
 */
export interface RouteHover {
  nodeId: number | null;
  hop: DirectedHop | null;
}

/**
 * The sidebar for a traceroute drawn on the map; hovering a row or its hop on
 * the map picks out the other.
 */
export function MapTraceroute({
  detail,
  drawing,
  error,
  hop,
  packetRowId,
  hover,
  onHover,
}: {
  detail: PacketDetail | null;
  drawing: TracerouteDrawing | null;
  error: string | null;
  hop: DirectedHop;
  packetRowId: number;
  hover: RouteHover | null;
  onHover: (hover: RouteHover | null) => void;
}) {
  if (detail === null || drawing === null)
    return <p className="muted">{error ?? "Loading traceroute…"}</p>;
  const nodes = new Map(detail.route_nodes.map((node) => [node.node_id, node]));
  const short = (nodeId: number) =>
    shortLabel(nodeId, nodes.get(nodeId)?.short_name ?? null);
  const long = (nodeId: number) => {
    const node = nodes.get(nodeId);
    return node === undefined ? formatNodeId(nodeId) : nodeLabel(node);
  };
  const ends = detail.traceroute;
  const placed = new Set(drawing.markers.map((marker) => marker.nodeId));
  const rows =
    ends === null || drawing.paths === null
      ? []
      : routeRows(drawing.paths, ends.requester_node_id, ends.target_node_id);
  return (
    <section className="selected-node map-traceroute" aria-live="polite">
      <div className="map-traceroute-heading">
        {ends === null ? (
          <h3>Traceroute</h3>
        ) : (
          <h3
            title={`${long(ends.requester_node_id)} → ${long(ends.target_node_id)}`}
          >
            {short(ends.requester_node_id)} → {short(ends.target_node_id)}
          </h3>
        )}
        <p className="muted">
          <ShortTime value={detail.packet.first_received_at} />
          {ends !== null &&
          detail.packet.sender_node_id === ends.target_node_id ? (
            <>
              {" "}
              <Badge>reply</Badge>
            </>
          ) : null}
        </p>
      </div>
      {rows.length === 0 ? (
        <p className="muted">No route reported.</p>
      ) : (
        <ol
          className="map-traceroute-route"
          aria-label="Route"
          onMouseLeave={() => onHover(null)}
        >
          {rows.map((row, index) =>
            row.kind === "gap" ? (
              <li key={index} className="map-traceroute-gap muted">
                ⋯ {row.text}
              </li>
            ) : row.kind === "back" ? (
              <li key={index} className="map-traceroute-gap muted">
                Back
              </li>
            ) : (
              <li
                key={index}
                className={[
                  row.unreached ? "unreached" : "",
                  sameHop(row.hop, hop) ? "map-traceroute-ours" : "",
                  hover !== null &&
                  ((row.nodeId !== null && row.nodeId === hover.nodeId) ||
                    sameHop(row.hop, hover.hop))
                    ? "highlighted"
                    : "",
                ].join(" ")}
                onMouseEnter={() =>
                  onHover({ nodeId: row.nodeId, hop: row.hop })
                }
              >
                <MarkerChip
                  label={row.label}
                  placed={row.nodeId !== null && placed.has(row.nodeId)}
                />
                {row.nodeId === null ? (
                  <span className="muted" title="Unknown relay">
                    ?
                  </span>
                ) : (
                  <span className="map-traceroute-name">
                    <strong>{short(row.nodeId)}</strong>{" "}
                    <span className="muted">{long(row.nodeId)}</span>
                  </span>
                )}
                {row.snr === undefined ? null : (
                  <HopSnr
                    className="number"
                    snr={row.snr}
                    spreadingFactor={detail.spreading_factor}
                    digits={1}
                  />
                )}
              </li>
            ),
          )}
        </ol>
      )}
      <p className="link-pages">
        <a
          className="text-button"
          href={packetHopHref(packetRowId, hop.fromNodeId, hop.toNodeId)}
        >
          All routes for this traceroute <Icon name="open" />
        </a>
      </p>
    </section>
  );
}
