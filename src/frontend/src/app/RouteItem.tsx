import { type ReactNode } from "react";

import {
  type HopCost,
  type RouteHopCost,
  type RoutePath,
  type TracerouteRoute,
} from "./api";
import { distanceMeters, type LatLon } from "./lineOfSight";
import { type DirectedHop } from "./minimapMarkers";
import {
  formatKm,
  formatNodeId,
  formatRole,
  lineOfSightHref,
  linkDetailsHref,
  tracerouteHopHref,
} from "./nodeDisplay";
import { DoubtfulBadge, HopSnr, Icon, NodeLink } from "./ui";

// A traceroute path as this gateway reported it: each arrow carries the SNR at
// the node it points to, after a dot in its minimap line's tier colour, and a
// 0 after it when the hop spent none; "?" is a relay that did not record itself, and a
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
  highlightedHop = null,
  onNodeHover,
  zeroCostHops,
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
  const isHop = (index: number, selected = hop) =>
    selected !== null &&
    index > 0 &&
    index - 1 < path.snr_db.length &&
    path.nodes[index - 1] === selected.fromNodeId &&
    path.nodes[index] === selected.toNodeId;
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
                  zeroCost={zeroCostHops.has(
                    `${path.nodes[index - 1]}-${path.nodes[index]}`,
                  )}
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
                    nodeId === highlightedNode ||
                    isHop(index, highlightedHop) ||
                    isHop(index + 1, highlightedHop)
                      ? "highlighted"
                      : "",
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
  /** The hops that spent none, as `${from}-${to}` (zeroCostHops). */
  zeroCostHops: ReadonlySet<string>;
}

/** A traceroute node or directed hop highlighted beside the minimap. */
export interface NodeHighlight {
  highlightedNode: number | null;
  highlightedHop?: DirectedHop | null;
  onNodeHover: (nodeId: number | null) => void;
}

function RouteHop({
  path,
  index,
  spreadingFactor,
  zeroCost,
}: {
  path: RoutePath;
  index: number;
  spreadingFactor: number;
  zeroCost: boolean;
}) {
  if (index >= path.snr_db.length) {
    return (
      <span className="route-hop muted" title="Not reached yet">
        ⋯
      </span>
    );
  }
  const snr = path.snr_db[index];
  return (
    <span className="route-hop">
      <HopSnr snr={snr} spreadingFactor={spreadingFactor} after=" →" />
      {/* A 0 dB step is no measured hop. */}
      {zeroCost && snr !== 0 ? (
        <span className="route-zero-cost" title="0-hop: no hop spent">
          0
        </span>
      ) : null}
    </span>
  );
}

/**
 * A route's reported steps, with distances and hops spent only for its
 * measured radio hops.
 */
export function RouteHopList({
  route: { path, path_hops: measuredHops, hop_costs: hopCosts },
  nodeNames,
  ends,
  placed,
  spreadingFactor,
  at,
  doubted,
  highlightedNode,
  highlightedHop = null,
  onNodeHover,
}: {
  route: TracerouteRoute;
  nodeNames: ReadonlyMap<number, string | null>;
  at: ReadonlyMap<number, LatLon>;
  /** The names of the hop's doubtful nodes, empty when none is. */
  doubted: (nodeIds: number[]) => string[];
} & Omit<RouteMarkers, "zeroCostHops"> &
  NodeHighlight) {
  const costs = new Map(
    hopCosts.map((cost) => [`${cost.from_node_id}-${cost.to_node_id}`, cost]),
  );
  const nodeName = (nodeId: number) =>
    nodeNames.get(nodeId) ?? formatNodeId(nodeId);
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
    return {
      index,
      from,
      to,
      snr,
      km,
      measured,
      cost: costs.get(`${from}-${to}`),
    };
  });
  const longest = Math.max(...hops.map((hop) => hop.km ?? -1));
  const node = (
    nodeId: number | null,
    index: number,
    hopHighlighted: boolean,
  ) => (
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
      highlighted={
        nodeId !== null && (nodeId === highlightedNode || hopHighlighted)
      }
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
          <th scope="col" className="number">
            Hops spent
          </th>
          <th scope="col">
            <span className="visually-hidden">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {hops.map(({ index, from, to, snr, km, measured, cost }) => {
          const hopHighlighted =
            highlightedHop !== null &&
            from === highlightedHop.fromNodeId &&
            to === highlightedHop.toNodeId;
          return (
            <tr key={index}>
              <td>
                {node(from, index, hopHighlighted)}
                <span className="muted"> → </span>
                {node(to, index + 1, hopHighlighted)}
              </td>
              <td>
                <HopSnr snr={snr} spreadingFactor={spreadingFactor} />
              </td>
              <td className="number">
                <span
                  className={km !== null && km === longest ? "longest" : ""}
                >
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
              <td className="number">
                {measured ? (
                  <HopsSpent
                    cost={cost}
                    firstHop={from === ends.requester || from === ends.target}
                    nodeName={nodeName}
                  />
                ) : null}
              </td>
              <td>
                {from !== null && to !== null && from !== to
                  ? (
                      [
                        [
                          linkDetailsHref("traceroute", from, to),
                          "Link details",
                          "links",
                        ],
                        [
                          tracerouteHopHref(from, to),
                          "All traceroutes with this hop",
                          "route",
                        ],
                        [lineOfSightHref(from, to), "Line of sight", "profile"],
                      ] as const
                    ).map(([href, label, icon]) => (
                      <a
                        key={icon}
                        className="icon-button"
                        href={href}
                        title={label}
                        aria-label={label}
                      >
                        <Icon name={icon} />
                      </a>
                    ))
                  : null}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// What a measured hop spent: this run's own, else ≈ when learned from other
// traceroutes or by role; ? when unknown.
function HopsSpent({
  cost,
  firstHop,
  nodeName,
}: {
  cost: RouteHopCost | undefined;
  firstHop: boolean;
  nodeName: (nodeId: number) => string;
}) {
  const [text, title] =
    cost === undefined || cost.spent === null
      ? ["?", "No traceroute showed whether it spends a hop"]
      : cost.learned === null
        ? [
            String(cost.spent),
            firstHop
              ? "A route's first hop always spends one"
              : "Shown by this traceroute's copies",
          ]
        : [`≈${cost.spent}`, learnedTitle(cost.learned, nodeName)];
  return (
    <span
      className={cost?.spent === 0 ? "route-zero-cost" : undefined}
      title={title}
    >
      {text}
    </span>
  );
}

function learnedTitle(
  { free, costly, verdict, role_node_id, role }: HopCost,
  nodeName: (nodeId: number) => string,
): string {
  const total = (free + costly).toLocaleString();
  return verdict === "free"
    ? `Free in ${free.toLocaleString()} of ${total} other traceroutes`
    : verdict === "costly"
      ? `Spent a hop in ${costly.toLocaleString()} of ${total} other traceroutes`
      : `By role: ${nodeName(role_node_id!)} is ${formatRole(role)}`;
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
