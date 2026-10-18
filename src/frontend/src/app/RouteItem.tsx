import { type ReactNode } from "react";

import { type RoutePath, type TracerouteRoute } from "./api";
import { distanceMeters, type LatLon } from "./lineOfSight";
import { type DirectedHop } from "./minimapMarkers";
import { formatKm } from "./nodeDisplay";
import { DoubtfulBadge, HopSnr, NodeLink } from "./ui";

// A traceroute path as this gateway reported it: each arrow carries the SNR at
// the node it points to, after a dot in its minimap line's tier colour; "?" is a relay that did not record itself, and a
// dotted arrow is a hop the packet had not made yet. Nodes show their long
// name when known, else their ID, after a chip with their minimap marker's
// label; the requester and target are set apart, as on the minimap. Hovering
// or focusing a node reports it, so its minimap marker can stand out; the
// highlighted node stands out here too. Searched nodes are ringed wherever
// they are, and so are both nodes of a hop a link asked for wherever the path
// measured it.
export function RouteItem({
  label,
  path,
  nodeNames,
  ends,
  firstNumber,
  placed,
  spreadingFactor,
  matched,
  hop = null,
  endExtra,
  highlightedNode = null,
  onNodeHover,
}: {
  label: string;
  path: RoutePath;
  nodeNames: ReadonlyMap<number, string | null>;
  /** Where this path's first node falls on its minimap numbering. */
  firstNumber: number;
  /** Nodes to mark as searched for. */
  matched?: ReadonlySet<number>;
  /** The hop to mark. */
  hop?: DirectedHop | null;
  /** Shown after the requester's and target's names. */
  endExtra?: (nodeId: number) => ReactNode;
} & RouteMarkers &
  Partial<NodeHighlight>) {
  // The hop from node index - 1 to node index is measured when it has an SNR slot.
  const isHop = (index: number) =>
    hop !== null &&
    index > 0 &&
    index - 1 < path.snr_db.length &&
    path.nodes[index - 1] === hop.fromNodeId &&
    path.nodes[index] === hop.toNodeId;
  return (
    <div className="route-item">
      <dt>{label}</dt>
      <dd>
        <ol className="route-path">
          {path.nodes.map((nodeId, index) => (
            <li key={index}>
              {index > 0 ? (
                <RouteHop
                  path={path}
                  index={index - 1}
                  spreadingFactor={spreadingFactor}
                />
              ) : null}
              {nodeId === null ? (
                <span className="route-node" title="Unknown relay">
                  <MarkerChip
                    label={String(firstNumber + index)}
                    placed={false}
                  />
                  <span className="muted">?</span>
                </span>
              ) : (
                <span
                  className={[
                    "route-node",
                    nodeId === highlightedNode ? "highlighted" : "",
                    matched?.has(nodeId) ? "searched-node" : "",
                    isHop(index) || isHop(index + 1) ? "hop-end" : "",
                    nodeId === ends.requester || nodeId === ends.target
                      ? "route-end"
                      : "",
                  ].join(" ")}
                  onMouseEnter={() => onNodeHover?.(nodeId)}
                  onMouseLeave={() => onNodeHover?.(null)}
                  onFocus={() => onNodeHover?.(nodeId)}
                  onBlur={() => onNodeHover?.(null)}
                >
                  <MarkerChip
                    label={
                      nodeId === ends.requester
                        ? "S"
                        : nodeId === ends.target
                          ? "R"
                          : String(firstNumber + index)
                    }
                    placed={placed.has(nodeId)}
                  />
                  <NodeLink nodeId={nodeId} name={nodeNames.get(nodeId)} />
                  {endExtra &&
                  (nodeId === ends.requester || nodeId === ends.target)
                    ? endExtra(nodeId)
                    : null}
                </span>
              )}
            </li>
          ))}
        </ol>
      </dd>
    </div>
  );
}

// A node's minimap marker label: S, R or its place along the route, dashed
// when the node has no marker.
function MarkerChip({ label, placed }: { label: string; placed: boolean }) {
  const end = label === "S" || label === "R";
  return (
    <span
      className={[
        "route-chip",
        end ? "hop-tone-sender" : "hop-tone-relay",
        placed ? "" : "unplaced",
      ].join(" ")}
      title={placed ? undefined : "No known position, not on the map"}
    >
      {label}
    </span>
  );
}

/** How the route text labels its nodes to match the minimap's markers. */
export interface RouteMarkers {
  /** The traceroute's requester (S) and target (R). */
  ends: { requester: number; target: number };
  /** The node IDs that have a marker on the minimap. */
  placed: ReadonlySet<number>;
  /** The packet's spreading factor, which sets each hop's tier. */
  spreadingFactor: number;
}

/** A traceroute node highlighted in the route text and on the minimap. */
export interface NodeHighlight {
  highlightedNode: number | null;
  onNodeHover: (nodeId: number | null) => void;
}

function RouteHop({
  path,
  index,
  spreadingFactor,
}: {
  path: RoutePath;
  index: number;
  spreadingFactor: number;
}) {
  if (index >= path.snr_db.length) {
    return (
      <span className="route-hop muted" title="Not reached yet">
        ⋯
      </span>
    );
  }
  return (
    <HopSnr
      className="route-hop"
      snr={path.snr_db[index]}
      spreadingFactor={spreadingFactor}
      after=" →"
    />
  );
}

/** A route's reported steps, with distances only for its measured radio hops. */
export function RouteHopList({
  route: { path, path_hops: measuredHops },
  nodeNames,
  ends,
  placed,
  spreadingFactor,
  at,
  doubted,
  highlightedNode,
  onNodeHover,
}: {
  route: TracerouteRoute;
  nodeNames: ReadonlyMap<number, string | null>;
  at: ReadonlyMap<number, LatLon>;
  /** The names of the hop's doubtful nodes, empty when none is. */
  doubted: (nodeIds: number[]) => string[];
} & RouteMarkers &
  NodeHighlight) {
  const hops = path.snr_db.map((snr, index) => {
    const from = path.nodes[index];
    const to = path.nodes[index + 1];
    const a = from === null ? undefined : at.get(from);
    const b = to === null ? undefined : at.get(to);
    const measured = measuredHops.some(
      (hop) =>
        hop.from_node_id === from &&
        hop.to_node_id === to &&
        hop.snr_db === snr,
    );
    const km =
      !measured || a === undefined || b === undefined
        ? null
        : distanceMeters(a, b) / 1000;
    return { index, from, to, snr, km };
  });
  const longest = Math.max(...hops.map((hop) => hop.km ?? -1));
  const node = (nodeId: number | null, index: number) => (
    <HopNode
      nodeId={nodeId}
      label={
        nodeId === ends.requester
          ? "S"
          : nodeId === ends.target
            ? "R"
            : String(index)
      }
      name={nodeId === null ? null : (nodeNames.get(nodeId) ?? null)}
      placed={nodeId !== null && placed.has(nodeId)}
      highlighted={nodeId !== null && nodeId === highlightedNode}
      onNodeHover={onNodeHover}
    />
  );
  return (
    <table className="hop-list">
      <thead>
        <tr>
          <th scope="col">Hop</th>
          <th scope="col">SNR</th>
          <th scope="col" className="number">
            km
          </th>
        </tr>
      </thead>
      <tbody>
        {hops.map(({ index, from, to, snr, km }) => (
          <tr
            key={index}
            className={
              highlightedNode !== null &&
              (from === highlightedNode || to === highlightedNode)
                ? "highlighted"
                : undefined
            }
          >
            <td>
              {node(from, index)}
              <span className="muted"> → </span>
              {node(to, index + 1)}
            </td>
            <td>
              <HopSnr snr={snr} spreadingFactor={spreadingFactor} />
            </td>
            <td className="number">
              <span className={km !== null && km === longest ? "longest" : ""}>
                {formatKm(km)}
              </span>
              <DoubtfulBadge
                suspects={
                  km === null
                    ? []
                    : doubted([from, to].filter((nodeId) => nodeId !== null))
                }
              />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function HopNode({
  nodeId,
  label,
  name,
  placed,
  highlighted,
  onNodeHover,
}: {
  nodeId: number | null;
  label: string;
  name: string | null;
  placed: boolean;
  highlighted: boolean;
  onNodeHover: (nodeId: number | null) => void;
}) {
  if (nodeId === null) {
    return (
      <span className="route-node" title="Unknown relay">
        <MarkerChip label={label} placed={false} />
        <span className="muted">?</span>
      </span>
    );
  }
  return (
    <span
      className={["route-node", highlighted ? "highlighted" : ""].join(" ")}
      onMouseEnter={() => onNodeHover(nodeId)}
      onMouseLeave={() => onNodeHover(null)}
      onFocus={() => onNodeHover(nodeId)}
      onBlur={() => onNodeHover(null)}
    >
      <MarkerChip label={label} placed={placed} />
      <NodeLink nodeId={nodeId} name={name} />
    </span>
  );
}
