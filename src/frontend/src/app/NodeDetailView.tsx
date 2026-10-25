import { useState } from "react";

import {
  type EstimatedPosition,
  type LinkTable,
  NodeDetail,
  NodeOverview,
  nodeLinksUrl,
  nodePacketsUrl,
  nodeUrl,
} from "./api";
import { AddContactButton } from "./AddContact";
import {
  formatHardware,
  formatNodeId,
  formatRole,
  mapHref,
  nodeLabel,
  nodeLinksHref,
  nodeTraceroutesHref,
  nodeMarkerLabel,
  estimateText,
  mapReportPositionText,
} from "./nodeDisplay";
import { now as clockNow } from "./clock";
import { NodeActivityCharts } from "./NodeActivityCharts";
import { CONTACT_DAYS } from "./nodeContacts";
import { NodeLinks } from "./NodeLinks";
import { NodePositionMinimap } from "./NodePositionMinimap";
import { PagedPacketTable, PortSelect } from "./PacketTable";
import { useApi } from "./useApi";
import { Badge, CopyValue, DataItem, Icon, StatusPanel } from "./ui";
import { formatHours } from "./zones";

export function NodeDetailView({ nodeId }: { nodeId: number }) {
  const [port, setPort] = useState<number | null>(null);
  // Ages are measured from when the page opened.
  const [now] = useState(clockNow);
  const { data: page, error } = useApi<NodeOverview>(nodeUrl(nodeId));
  // For the Links table and the minimap's contact lines.
  const [since] = useState(
    () => new Date(now - CONTACT_DAYS * 24 * 60 * 60 * 1000),
  );
  const links = useApi<LinkTable>(nodeLinksUrl(nodeId, since));

  return (
    <section className="node-detail-page" aria-labelledby="node-heading">
      <a className="return-link" href="#nodes">
        <Icon name="back" />
        Back to nodes
      </a>
      {page === null && error === null ? (
        <StatusPanel>Loading node…</StatusPanel>
      ) : null}
      {error !== null ? <StatusPanel tone="error">{error}</StatusPanel> : null}
      {page !== null ? (
        <>
          <div className="detail-heading">
            <div className="selected-node-heading">
              <span className="node-avatar avatar-green" title="Short name">
                {nodeMarkerLabel(page.node ?? noNodeInfo(nodeId))}
              </span>
              <div>
                <p className="eyebrow">Node</p>
                <h2 id="node-heading">
                  {nodeLabel(page.node ?? noNodeInfo(nodeId))}
                </h2>
                <span className="mono muted">{formatNodeId(nodeId)}</span>
                {page.node?.last_gateway_at == null ? null : (
                  <>
                    {" "}
                    <GatewayBadge at={page.node.last_gateway_at} now={now} />
                  </>
                )}
              </div>
            </div>
            <nav className="node-links" aria-label="This node elsewhere">
              {page.node?.position != null || page.node?.estimate != null ? (
                <a href={mapHref(nodeId)}>
                  <Icon name="map" />
                  Show on map
                </a>
              ) : null}
              <a href={nodeLinksHref(nodeId)}>
                <Icon name="links" />
                Links
              </a>
              <a href={nodeTraceroutesHref(nodeId)}>
                <Icon name="route" />
                Traceroutes
              </a>
              {page.node?.contact_url == null ? null : (
                <AddContactButton
                  url={page.node.contact_url}
                  name={nodeLabel(page.node)}
                  nodeId={nodeId}
                />
              )}
            </nav>
          </div>
          {page.node === null ? (
            <StatusPanel>
              Nothing current is known about this node: it has not sent anything
              in the saved history.
            </StatusPanel>
          ) : (
            <>
              <NodeSummary node={page.node} links={links.data} now={now} />
              <NodeActivityCharts nodeId={nodeId} />
            </>
          )}
          <NodeLinks nodeId={nodeId} links={links} since={since} />
          <div className="section-intro compact heading-with-filter">
            <h3>Packets</h3>
            <PortSelect ports={page.ports} port={port} onChange={setPort} />
          </div>
          <PagedPacketTable
            url={nodePacketsUrl(nodeId, port)}
            empty={
              port === null
                ? "No packets from or to this node are saved."
                : "No packets from or to this node on this port are saved."
            }
          />
        </>
      ) : null}
    </section>
  );
}

function NodeSummary({
  node,
  links,
  now,
}: {
  node: NodeDetail;
  links: LinkTable | null;
  now: number;
}) {
  return (
    <div className="node-facts">
      <dl>
        <DataItem label="Long name" value={node.long_name ?? "Not reported"} />
        <DataItem
          label="Short name"
          value={node.short_name ?? "Not reported"}
        />
        <DataItem
          label="Node ID"
          value={
            <CopyValue value={formatNodeId(node.node_id)} label="node ID" />
          }
        />
        <DataItem
          label="Node number"
          value={<CopyValue value={String(node.node_id)} label="node number" />}
        />
        <DataItem label="Role" value={formatRole(node.role)} />
        <DataItem label="Channel" value={node.channel ?? "Unknown"} />
        <DataItem
          label="Last seen"
          value={
            <>
              <span className="data-line">
                <Age now={now} at={node.last_seen_at} bands={SEEN_BANDS} />
              </span>
              <span className="data-line muted">{seenReason(node)}</span>
            </>
          }
        />
        <DataItem
          label="Last packet sent"
          value={
            node.last_sent_at === null ? (
              "No own packet observed"
            ) : (
              <Age
                now={now}
                at={node.last_sent_at}
                packetRowId={node.last_sent_packet_row_id}
                bands={SENT_BANDS}
              />
            )
          }
        />
        <DataItem
          label="NodeInfo received"
          value={
            node.node_info_observed_at === null ? (
              "Not reported"
            ) : (
              <Age
                now={now}
                at={node.node_info_observed_at}
                packetRowId={node.node_info_packet_row_id}
              />
            )
          }
        />
        <DataItem
          label="Position"
          value={<PositionText node={node} now={now} />}
        />
        <DataItem
          label="Hardware"
          value={formatHardware(node.hardware_model)}
        />
        <DataItem
          label={
            node.map_report?.firmware_version
              ? "Firmware"
              : "Firmware (estimate)"
          }
          value={
            node.map_report?.firmware_version ? (
              <>
                <span className="data-line mono">
                  {node.map_report.firmware_version}
                </span>
                <span className="data-line muted">From its map report</span>
              </>
            ) : node.firmware === null ? (
              "Unknown"
            ) : (
              <>
                <span className="data-line">
                  {node.firmware.minimum} or newer
                </span>
                <span className="data-line muted">{node.firmware.reason}</span>
              </>
            )
          }
        />
        <DataItem
          label="Gateway uploads"
          value={
            node.last_gateway_at === null ? (
              <span className="muted">No uploads seen</span>
            ) : (
              <>
                Last upload{" "}
                <Age now={now} at={node.last_gateway_at} bands={SEEN_BANDS} />
              </>
            )
          }
        />
        <DataItem
          label="Map report"
          value={
            node.map_report === null ? (
              "None received"
            ) : (
              <>
                <span className="data-line">
                  {formatRole(node.map_report.modem_preset)}
                  {node.map_report.region === null
                    ? ", region not set"
                    : `, ${node.map_report.region}`}
                </span>
                <span className="data-line">
                  Heard {node.map_report.nodes_online_local.toLocaleString()}{" "}
                  {node.map_report.nodes_online_local === 1 ? "node" : "nodes"}{" "}
                  locally in 2 hours
                </span>
                <span className="data-line muted">
                  <Age now={now} at={node.map_report.received_at} />
                </span>
              </>
            )
          }
        />
      </dl>
      <NodePositionMinimap node={node} links={links} />
    </div>
  );
}

/** The node's current position: reported, from its map report, or estimated. */
function PositionText({ node, now }: { node: NodeDetail; now: number }) {
  if (node.estimate !== null) {
    return <EstimatedPositionItem estimate={node.estimate} />;
  }
  if (node.position === null) {
    return "Not reported";
  }
  if (node.position.from_map_report) {
    return (
      <>
        <span className="data-line mono">
          ≈ {node.position.latitude.toFixed(3)},{" "}
          {node.position.longitude.toFixed(3)}
        </span>
        <span className="data-line muted">
          {mapReportPositionText(node.position.precision_km)}{" "}
          <Age now={now} at={node.position.observed_at} />
        </span>
      </>
    );
  }
  return (
    <>
      <span className="data-line mono">
        {node.position.latitude.toFixed(5)},{" "}
        {node.position.longitude.toFixed(5)}
      </span>
      {node.position.altitude_meters == null ? null : (
        <span className="data-line">
          Altitude {node.position.altitude_meters} m
        </span>
      )}
      {node.location_source === null ? null : (
        <span className="data-line">
          {LOCATION_SOURCES[node.location_source] ?? node.location_source}
        </span>
      )}
      <span className="data-line muted">
        <Age
          now={now}
          at={node.position.observed_at}
          packetRowId={node.position.packet_row_id}
        />
      </span>
    </>
  );
}

// What a position packet's location_source says about where it came from.
const LOCATION_SOURCES: Record<string, string> = {
  LOC_MANUAL: "Set by hand",
  LOC_INTERNAL: "From the node's own GPS",
  LOC_EXTERNAL: "From an external GPS, such as a phone",
};

// A node with no reported position, placed by its neighbours.
function EstimatedPositionItem({ estimate }: { estimate: EstimatedPosition }) {
  return (
    <>
      <span className="data-line mono">
        ≈ {estimate.latitude.toFixed(3)}, {estimate.longitude.toFixed(3)}
      </span>
      <span className="data-line muted">{estimateText(estimate)}</span>
    </>
  );
}

// Hours after which a time turns amber, then red. A node with default
// settings sends device telemetry every 30 minutes, so an hour without being
// seen is already unusual; own packets can be hours apart on a quiet node.
const SEEN_BANDS = { amber: 1, red: 3 };
const SENT_BANDS = { amber: 24, red: 72 };

type Bands = { amber: number; red: number };

function hoursAgo(now: number, at: string): number {
  return Math.max(0, (now - new Date(at).getTime()) / 3_600_000);
}

function ageTone(hours: number, bands: Bands): "green" | "amber" | "red" {
  return hours >= bands.red ? "red" : hours >= bands.amber ? "amber" : "green";
}

/**
 * Shown only for a node that has uploaded what it heard to MQTT, coloured by
 * how recently, so a radio that still sends but whose MQTT connection is down
 * stands out.
 */
function GatewayBadge({ at, now }: { at: string; now: number }) {
  const hours = hoursAgo(now, at);
  return (
    <Badge
      tone={ageTone(hours, SEEN_BANDS)}
      title={`Uploads what it hears to MQTT. Last upload ${formatHours(hours)} ago.`}
    >
      Gateway
    </Badge>
  );
}

/**
 * How long ago `at` was, with the full time on hover, linked to the packet when
 * there is one, and coloured by `bands` when given.
 */
function Age({
  now,
  at,
  packetRowId = null,
  bands = null,
}: {
  now: number;
  at: string;
  packetRowId?: number | null;
  bands?: Bands | null;
}) {
  const time = new Date(at);
  const hours = hoursAgo(now, at);
  const tone = bands === null ? "age-plain" : `age-${ageTone(hours, bands)}`;
  const text = (
    <time dateTime={at} title={time.toLocaleString()}>
      {formatHours(hours)} ago
    </time>
  );
  return packetRowId === null ? (
    <span className={tone}>{text}</span>
  ) : (
    <a className={`text-button ${tone}`} href={`#packets/${packetRowId}`}>
      {text}
    </a>
  );
}

/**
 * Which kind of activity set last_seen_at: it is the latest of the node's own
 * packet, its gateway uploads and traceroute hops or map reports, so matching
 * times tell which one it was.
 */
function seenReason(node: NodeDetail): string {
  if (node.last_sent_at === node.last_seen_at)
    return "Sent a packet of its own";
  if (node.last_gateway_at === node.last_seen_at)
    return "Uploaded what it heard, as a gateway";
  return "Appeared on a traceroute route or sent a map report";
}

function noNodeInfo(nodeId: number) {
  return { node_id: nodeId, long_name: null, short_name: null };
}
