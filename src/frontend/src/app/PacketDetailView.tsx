import { useMemo, useState, type MouseEvent } from "react";

import {
  ReplyRoute,
  PacketDetail,
  PacketReception,
  RoutePath,
  packetUrl,
} from "./api";
import {
  BROADCAST_NODE_ID,
  formatKm,
  formatNodeId,
  hopGroup,
  hopTone,
  nodeName,
} from "./nodeDisplay";
import { type MinimapFocus, PacketMinimap } from "./PacketMinimap";
import {
  type DirectedHop,
  minimapMarkers,
  zeroCostHops,
} from "./minimapMarkers";
import { type NodeHighlight, RouteItem, type RouteMarkers } from "./RouteItem";
import {
  type TracerouteGroup,
  samePath,
  sharedForwardRoute,
  splitGroups,
  tracerouteGroups,
} from "./tracerouteRows";
import { useApi } from "./useApi";
import {
  DataItem,
  IconButton,
  NodeLink,
  RelayCell,
  ReturnLink,
  StatusPanel,
  ShortTime,
} from "./ui";

export function PacketDetailView({
  packetRowId,
  hop = null,
  openedFrom = null,
}: {
  packetRowId: number;
  /** List the reports that crossed this hop first, with the rest collapsed. */
  hop?: DirectedHop | null;
  /** The page this one was opened from, for a link back. */
  openedFrom?: { href: string; label: string } | null;
}) {
  const { data: detail, error } = useApi<PacketDetail>(packetUrl(packetRowId));

  return (
    <section aria-labelledby="detail-heading">
      {openedFrom !== null ? (
        <ReturnLink
          href={openedFrom.href}
          label={`Back to ${openedFrom.label}`}
        />
      ) : null}
      {detail === null && error === null ? (
        <StatusPanel>Loading packet detail…</StatusPanel>
      ) : null}
      {error !== null ? <StatusPanel tone="error">{error}</StatusPanel> : null}
      {detail !== null ? (
        <PacketDetailContent detail={detail} hop={hop} />
      ) : null}
    </section>
  );
}

function PacketDetailContent({
  detail,
  hop,
}: {
  detail: PacketDetail;
  hop: DirectedHop | null;
}) {
  const { packet, receptions } = detail;
  const minimap = useMemo(() => minimapMarkers(detail, hop), [detail, hop]);
  const { groups, hidden } = useMemo(
    () =>
      detail.traceroute === null
        ? { groups: groupByHops(receptions), hidden: 0 }
        : tracerouteGroups(detail),
    [detail, receptions],
  );
  const sharedForward = useMemo(() => sharedForwardRoute(detail), [detail]);
  // Offsets count from the first report, even when it is hidden beneath a
  // longer route.
  const earliest = useMemo(
    () => Math.min(...receptions.map((r) => Date.parse(r.received_at))),
    [receptions],
  );
  const nodeNames = useMemo(
    () =>
      new Map(
        detail.route_nodes.map((node): [number, string | null] => [
          node.node_id,
          nodeName(node),
        ]),
      ),
    [detail],
  );
  // A page opened on a hop lists the reports with it first and collapses the
  // rest, and its minimap shows their routes, until the rest are shown. With
  // no listed report crossing it, everything is shown as usual.
  const hopIds = minimap.hopReports?.receptionIds;
  const narrowed = hop !== null && hopIds !== undefined && hopIds.size > 0;
  const [othersShown, setOthersShown] = useState(false);
  const split = useMemo(
    () => (narrowed ? splitGroups(groups, hopIds) : null),
    [narrowed, groups, hopIds],
  );
  // Hovering a reception (its row, or its gateway on the minimap) previews it
  // on the minimap and in the table over whatever is pinned;
  // clicking a reception or a relay pins it, and clicking it again unpins it.
  const [hovered, setHovered] = useState<number | null>(null);
  const [pinned, setPinned] = useState<MinimapFocus>(null);
  const pinnedReception =
    pinned?.kind === "reception" ? pinned.receptionId : null;
  const pinnedRoutes =
    pinned?.kind === "relay"
      ? minimap.relayReports.get(pinned.nodeId)?.receptionIds
      : undefined;
  const focus: MinimapFocus =
    hovered !== null
      ? { kind: "reception", receptionId: hovered }
      : (pinned ?? (narrowed && !othersShown ? { kind: "hop", ...hop } : null));
  const hopName = (nodeId: number) =>
    nodeNames.get(nodeId) ?? formatNodeId(nodeId);
  // A traceroute node hovered or focused in the route text or on the minimap,
  // highlighted in both. The route text also keeps the pinned relay
  // highlighted; the minimap already rings it.
  const [highlightedNode, setHighlightedNode] = useState<number | null>(null);
  const highlight = { highlightedNode, onNodeHover: setHighlightedNode };
  const routeHighlight = {
    ...highlight,
    highlightedNode:
      highlightedNode ?? (pinned?.kind === "relay" ? pinned.nodeId : null),
  };
  // The route text labels each node like its marker. Only traceroutes have
  // route text, so the ends of other packets are never used.
  const routeMarkers: RouteMarkers = useMemo(
    () => ({
      ends: {
        requester: detail.traceroute?.requester_node_id ?? -1,
        target: detail.traceroute?.target_node_id ?? -1,
      },
      placed: new Set(minimap.markers.map((marker) => marker.nodeId)),
      spreadingFactor: detail.spreading_factor,
      zeroCostHops: zeroCostHops(detail.hop_costs),
    }),
    [detail, minimap],
  );
  return (
    <>
      <div className="packet-heading">
        <h2 id="detail-heading">Packet {packet.packet_id}</h2>
        <span>{packet.portnum ?? "Unknown application"}</span>
        <span>
          Reported by {packet.reception_count}{" "}
          {packet.reception_count === 1 ? "gateway" : "gateways"}
        </span>
      </div>
      <div className="packet-top">
        <dl className="packet-identity">
          <DataItem
            label="Sender"
            value={
              <NodeNameAndId
                nodeId={packet.sender_node_id}
                name={packet.sender_name}
              />
            }
          />
          <DataItem
            label="Recipient"
            value={
              <NodeNameAndId
                nodeId={packet.recipient_node_id}
                name={packet.recipient_name}
              />
            }
          />
          <DataItem label="Channel" value={packet.channel} />
          <DataItem label="Packet ID" value={String(packet.packet_id)} />
          <DataItem label="Application" value={packet.portnum ?? "Unknown"} />
          <DataItem
            label="Heard"
            value={<ShortTime value={packet.first_received_at} />}
          />
        </dl>
        <div className="packet-payload">
          <h3>Payload</h3>
          {detail.payload === null ? (
            <p className="muted">
              Not readable
              {detail.unreadable_reason_description !== null
                ? `: ${detail.unreadable_reason_description}`
                : null}
            </p>
          ) : (
            <code>{detail.payload}</code>
          )}
        </div>
      </div>
      <div className="packet-lower">
        <div>
          {hop !== null ? (
            <p className="hop-note">
              {narrowed
                ? `Hop ${hopName(hop.fromNodeId)} → ${hopName(hop.toNodeId)}: ${hopIds.size} of ${minimap.reports} reports, listed first.`
                : `No report has the hop ${hopName(hop.fromNodeId)} → ${hopName(hop.toNodeId)}.`}
            </p>
          ) : null}
          {sharedForward !== null ? (
            <dl className="shared-route">
              <RouteItem
                label="Route to the target"
                path={sharedForward}
                nodeNames={nodeNames}
                firstNumber={0}
                hop={hop}
                {...routeHighlight}
                {...routeMarkers}
              />
            </dl>
          ) : null}
          <ReceptionTable
            groups={split?.matching ?? groups}
            sharedForward={sharedForward}
            sender={{
              nodeId: packet.sender_node_id,
              estimated: detail.sender_estimated,
            }}
            others={
              split === null
                ? null
                : {
                    groups: split.others,
                    shown: othersShown,
                    onToggle: () => setOthersShown((shown) => !shown),
                  }
            }
            hop={hop}
            earliest={earliest}
            nodeNames={nodeNames}
            hovered={hovered}
            {...routeHighlight}
            {...routeMarkers}
            pinned={(id) => id === pinnedReception || !!pinnedRoutes?.has(id)}
            onHover={setHovered}
            onPin={(id) =>
              setPinned(
                id === pinnedReception
                  ? null
                  : { kind: "reception", receptionId: id },
              )
            }
          />
          <p className="end-note">
            {receptions.length < packet.reception_count
              ? `Showing the first ${receptions.length} of ${packet.reception_count} gateway reports.`
              : "All gateway reports shown."}{" "}
            {hidden > 0
              ? `${hidden} ${hidden === 1 ? "report repeats" : "reports repeat"} one shown or only its start, and ${hidden === 1 ? "is" : "are"} hidden. `
              : null}
            Times are when reports reached the server.
            {minimap.markers.length === 0
              ? detail.traceroute === null
                ? " No map: neither the sender nor any gateway has a known position."
                : " No map: no node on the traceroute has a known position."
              : null}
          </p>
        </div>
        {minimap.markers.length > 0 ? (
          <PacketMinimap
            contents={minimap}
            focus={focus}
            onClearFocus={() => setOthersShown(true)}
            onRelayClick={(nodeId) =>
              setPinned((current) =>
                current?.kind === "relay" && current.nodeId === nodeId
                  ? null
                  : { kind: "relay", nodeId },
              )
            }
            {...(detail.traceroute !== null ? highlight : {})}
            onReceptionHover={setHovered}
          />
        ) : null}
      </div>
    </>
  );
}

function NodeNameAndId({
  nodeId,
  name,
}: {
  nodeId: number;
  name: string | null;
}) {
  return (
    <>
      <NodeLink nodeId={nodeId} name={name} />
      {name !== null && nodeId !== BROADCAST_NODE_ID ? (
        <span className="mono muted"> {formatNodeId(nodeId)}</span>
      ) : null}
    </>
  );
}

// One row per gateway, under a heading row per hop group; on a traceroute
// request, the route from its reply may lead. A reception's rows carry its ID,
// so the table reports which one the pointer is over or clicked.
function ReceptionTable({
  groups,
  others,
  sharedForward,
  sender,
  hop,
  earliest,
  nodeNames,
  hovered,
  pinned,
  onHover,
  onPin,
  ...highlight
}: {
  groups: TracerouteGroup[];
  /** The reports without the page's hop, listed after a toggle; null when not split. */
  others: {
    groups: TracerouteGroup[];
    shown: boolean;
    onToggle: () => void;
  } | null;
  /** On a reply, the route to the target shown above the table; rows show
   * theirs only when it differs. */
  sharedForward: RoutePath | null;
  sender: ReportSender;
  /** The hop to mark in each route. */
  hop: DirectedHop | null;
  /** When the first report arrived, including hidden ones, in milliseconds. */
  earliest: number;
  nodeNames: ReadonlyMap<number, string | null>;
  hovered: number | null;
  /** Whether a reception is pinned, or on a pinned relay's route. */
  pinned: (receptionId: number) => boolean;
  onHover: (receptionId: number | null) => void;
  onPin: (receptionId: number) => void;
} & NodeHighlight &
  RouteMarkers) {
  if (groups.length === 0) {
    return <StatusPanel>No gateway reports saved.</StatusPanel>;
  }
  const otherCount =
    others?.groups.reduce((count, group) => count + group.rows.length, 0) ?? 0;
  const renderGroups = (groups: TracerouteGroup[], part: string) =>
    groups.map(({ label, tone, rows }) => (
      <tbody key={`${part}-${label}`}>
        <tr className={`hop-group-row ${tone}`}>
          <th colSpan={6} scope="colgroup">
            {label} <span className="muted">· {rows.length}</span>
          </th>
        </tr>
        {rows.map((row) =>
          row.kind === "reception" ? (
            <ReceptionRows
              key={row.reception.reception_id}
              reception={row.reception}
              alsoReported={row.alsoReported}
              sharedForward={sharedForward}
              sender={sender}
              earliest={earliest}
              nodeNames={nodeNames}
              hovered={row.reception.reception_id === hovered}
              pinned={pinned(row.reception.reception_id)}
              hop={hop}
              {...highlight}
            />
          ) : (
            <ReplyRows
              key="reply"
              reply={row.reply}
              alsoReported={row.alsoReported}
              nodeNames={nodeNames}
              hovered={row.reply.reception_id === hovered}
              pinned={pinned(row.reply.reception_id)}
              hop={hop}
              {...highlight}
            />
          ),
        )}
      </tbody>
    ));

  return (
    <div className="table-wrap">
      <table
        className="reception-table"
        onMouseOver={(event) => onHover(eventReceptionId(event))}
        onMouseLeave={() => onHover(null)}
        onClick={(event) => {
          const id = eventReceptionId(event);
          // Links in the row still just open their node.
          if (id !== null && !(event.target as Element).closest("a")) {
            onPin(id);
          }
        }}
      >
        <thead>
          <tr>
            <th scope="col">Gateway</th>
            <th scope="col" className="number">
              SNR
            </th>
            <th scope="col" className="number">
              RSSI
            </th>
            <th scope="col">Relay</th>
            <th scope="col" className="number">
              Time
            </th>
            <th scope="col">
              <span className="visually-hidden">Pin</span>
            </th>
          </tr>
        </thead>
        {renderGroups(groups, "matching")}
        {others !== null && others.groups.length > 0 ? (
          <tbody>
            <tr className="others-row">
              <td colSpan={6}>
                <button
                  type="button"
                  className="text-button"
                  aria-expanded={others.shown}
                  onClick={others.onToggle}
                >
                  {others.shown ? "Hide" : "Show"} {otherCount}{" "}
                  {otherCount === 1 ? "report" : "reports"} without the hop
                </button>
              </td>
            </tr>
          </tbody>
        ) : null}
        {others?.shown ? renderGroups(others.groups, "others") : null}
      </table>
    </div>
  );
}

function eventReceptionId(event: MouseEvent): number | null {
  const row = (event.target as Element).closest<HTMLElement>(
    "tr[data-reception-id]",
  );
  return row === null ? null : Number(row.dataset.receptionId);
}

// The API already lists receptions in group order, so each group is a run.
function groupByHops(receptions: PacketReception[]): TracerouteGroup[] {
  const groups: TracerouteGroup[] = [];
  for (const reception of receptions) {
    const label = hopGroup(reception);
    const row = { kind: "reception" as const, reception, alsoReported: 0 };
    const last = groups.at(-1);
    if (last?.label === label) last.rows.push(row);
    else groups.push({ label, tone: hopTone(reception), rows: [row] });
  }
  return groups;
}

function rowProps(receptionId: number, hovered: boolean, pinned: boolean) {
  return {
    "data-reception-id": receptionId,
    className: [
      "reception-row",
      hovered ? "hovered" : "",
      pinned ? "pinned" : "",
    ].join(" "),
  };
}

// The button that pins a report's row and its marks on the minimap. Its click
// reaches the table's handler, which pins whichever row was clicked.
function PinCell({ pinned }: { pinned: boolean }) {
  return (
    <td className="pin-cell">
      <IconButton icon="pin" label="Pin on the map" aria-pressed={pinned} />
    </td>
  );
}

// How far the gateway was from the sender.
function ReportKm({
  reception,
  sender,
}: {
  reception: PacketReception;
  sender: ReportSender;
}) {
  if (reception.km === null) return null;
  const rough = sender.estimated || reception.gateway_estimated;
  const text = `${rough ? "~" : ""}${formatKm(reception.km)} km`;
  return (
    <span className="muted">
      {" · "}
      <span
        title={
          reception.direct
            ? "Heard directly"
            : "From the sender, though relays carried it"
        }
      >
        {text}
      </span>
    </span>
  );
}

// How many hidden reports repeat this route or only its start.
function AlsoReported({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <span
      className="muted"
      title="Gateways that reported the same or only its start"
    >
      {" "}
      +{count} {count === 1 ? "gateway" : "gateways"}
    </span>
  );
}

// The route a request's reply says reached the target. It shares the pin and
// hover of the reply report it came from.
function ReplyRows({
  reply,
  alsoReported,
  nodeNames,
  hovered,
  pinned,
  hop,
  ...highlight
}: {
  reply: ReplyRoute;
  alsoReported: number;
  nodeNames: ReadonlyMap<number, string | null>;
  hovered: boolean;
  pinned: boolean;
  hop: DirectedHop | null;
} & NodeHighlight &
  RouteMarkers) {
  const props = rowProps(reply.reception_id, hovered, pinned);
  return (
    <>
      <tr {...props}>
        <td className="gateway-cell">
          <div>
            <a href={`#packets/${reply.packet_row_id}`}>From the reply</a>
            <AlsoReported count={alsoReported} />
          </div>
        </td>
        <td className="number">—</td>
        <td className="number">—</td>
        <td className="mono">—</td>
        <td className="number">—</td>
        <PinCell pinned={pinned} />
      </tr>
      <tr {...props} className={`${props.className} route-row`}>
        <td colSpan={6}>
          <dl>
            <RouteItem
              label="Route to the target"
              path={reply.path}
              nodeNames={nodeNames}
              firstNumber={0}
              hop={hop}
              {...highlight}
            />
          </dl>
        </td>
      </tr>
    </>
  );
}

/** The packet's sender, for each report's distance from it. */
interface ReportSender {
  nodeId: number;
  /** Placed by an estimate, so distances from it are rough. */
  estimated: boolean;
}

function ReceptionRows({
  reception,
  alsoReported,
  sharedForward,
  sender,
  earliest,
  nodeNames,
  hovered,
  pinned,
  hop,
  ...highlight
}: {
  reception: PacketReception;
  alsoReported: number;
  sharedForward: RoutePath | null;
  sender: ReportSender;
  earliest: number;
  nodeNames: ReadonlyMap<number, string | null>;
  hovered: boolean;
  pinned: boolean;
  hop: DirectedHop | null;
} & NodeHighlight &
  RouteMarkers) {
  const offset = (Date.parse(reception.received_at) - earliest) / 1000;
  const props = rowProps(reception.reception_id, hovered, pinned);
  const paths = reception.paths;
  const forward =
    paths === null ||
    (sharedForward !== null && samePath(paths.forward, sharedForward))
      ? null
      : paths.forward;
  return (
    <>
      <tr {...props}>
        <td className="gateway-cell">
          <div>
            <NodeLink
              nodeId={reception.gateway_node_id}
              name={reception.gateway_name}
              title={reception.gateway_name ?? undefined}
            />
            {reception.gateway_name !== null ? (
              <span className="mono muted">
                {" "}
                {formatNodeId(reception.gateway_node_id)}
              </span>
            ) : null}
            <AlsoReported count={alsoReported} />
            <ReportKm reception={reception} sender={sender} />
          </div>
        </td>
        <td className="number">{reception.rx_snr ?? "—"}</td>
        <td className="number">{reception.rx_rssi ?? "—"}</td>
        <RelayCell reception={reception} />
        <td className="number">
          <time
            dateTime={reception.received_at}
            title={new Date(reception.received_at).toLocaleString()}
          >
            +{offset.toFixed(1)} s
          </time>
        </td>
        <PinCell pinned={pinned} />
      </tr>
      {paths !== null && (forward !== null || paths.return_path !== null) ? (
        <tr {...props} className={`${props.className} route-row`}>
          <td colSpan={6}>
            <dl>
              {forward !== null ? (
                <RouteItem
                  label="Route to target"
                  path={forward}
                  nodeNames={nodeNames}
                  firstNumber={0}
                  hop={hop}
                  {...highlight}
                />
              ) : null}
              {paths.return_path !== null ? (
                <RouteItem
                  label="Route back"
                  path={paths.return_path}
                  nodeNames={nodeNames}
                  firstNumber={paths.forward.nodes.length - 1}
                  hop={hop}
                  {...highlight}
                />
              ) : null}
            </dl>
          </td>
        </tr>
      ) : null}
    </>
  );
}
