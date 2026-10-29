import { useState, type ReactNode } from "react";

import {
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
  nodeHref,
  radioName,
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
import type { NodeLinkView } from "./routes";
import { PagedPacketTable, PortSelect } from "./PacketTable";
import {
  type NodeRadios,
  type RadioFilter,
  RadioCards,
  RadioChips,
  RadioItems,
} from "./radios";
import { useApi } from "./useApi";
import { Badge, CopyValue, DataItem, Icon, StatusPanel } from "./ui";
import { formatHours } from "./zones";

/** `linkView`, from a shared link's query, sets up its position minimap. */
export function NodeDetailView({
  nodeId,
  linkView = null,
}: {
  nodeId: number;
  linkView?: NodeLinkView | null;
}) {
  const [port, setPort] = useState<number | null>(null);
  // Not remembered: radio numbers change as reports expire.
  const [radio, setRadio] = useState<RadioFilter | null>(null);
  // Ages are measured from when the page opened.
  const [now] = useState(clockNow);
  const { data: page, error } = useApi<NodeOverview>(nodeUrl(nodeId));
  // For the Links table and the minimap's contact lines.
  const [since] = useState(
    () => new Date(now - CONTACT_DAYS * 24 * 60 * 60 * 1000),
  );
  const links = useApi<LinkTable>(nodeLinksUrl(nodeId, since));
  const radios = page?.radios ?? null;
  const shared = radios?.shared === true ? radios : null;

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
                <p className="eyebrow">
                  {shared === null ? "Node" : "Shared node number"}
                </p>
                <h2 id="node-heading">
                  {shared === null
                    ? nodeLabel(page.node ?? noNodeInfo(nodeId))
                    : formatNodeId(nodeId)}
                </h2>
                {shared === null ? (
                  <span className="mono muted">{formatNodeId(nodeId)}</span>
                ) : (
                  <span className="muted">
                    {shared.items.map(radioName).join(", ")}
                  </span>
                )}
                <OtherNumbers label="earlier" nodeIds={page.earlier_node_ids} />
                <OtherNumbers label="later" nodeIds={page.later_node_ids} />
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
              {page.node?.contact_url == null || shared !== null ? null : (
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
              <NodeSummary
                node={page.node}
                radios={radios}
                links={links.data}
                now={now}
                linkView={linkView}
              />
              {shared === null ? null : <RadioCards radios={shared} />}
              <NodeActivityCharts nodeId={nodeId} />
            </>
          )}
          <NodeLinks nodeId={nodeId} links={links} since={since} />
          <div className="section-intro compact heading-with-filter">
            <h3>Packets</h3>
            <PortSelect ports={page.ports} port={port} onChange={setPort} />
          </div>
          {shared === null ? null : (
            <RadioChips radios={shared} radio={radio} onChange={setRadio} />
          )}
          <PagedPacketTable
            url={nodePacketsUrl(nodeId, port, shared === null ? null : radio)}
            empty={
              port === null && radio === null
                ? "No packets from or to this node are saved."
                : "No packets match these filters."
            }
          />
        </>
      ) : null}
    </section>
  );
}

function NodeSummary({
  node,
  radios,
  links,
  now,
  linkView,
}: {
  node: NodeDetail;
  radios: NodeRadios | null;
  links: LinkTable | null;
  now: number;
  linkView: NodeLinkView | null;
}) {
  // A shared number's facts mix its radios.
  const note = radios?.shared === true ? "latest from any radio" : undefined;
  return (
    <div className="node-facts">
      <div className="fact-groups">
        <FactGroup title="Identity" note={note}>
          <DataItem
            label="Long name"
            value={node.long_name ?? "Not reported"}
          />
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
            value={
              <CopyValue value={String(node.node_id)} label="node number" />
            }
          />
          <DataItem label="Role" value={formatRole(node.role)} />
          <DataItem label="Channel" value={node.channel ?? "Unknown"} />
        </FactGroup>
        <FactGroup title="Activity">
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
        </FactGroup>
        <FactGroup title="Position" note={note}>
          <PositionItems node={node} now={now} />
        </FactGroup>
        {radios === null || radios.shared ? null : (
          <FactGroup title="Radios">
            <RadioItems radios={radios} />
          </FactGroup>
        )}
        <FactGroup title="Device" note={note}>
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
                  <span className="data-line muted">
                    {node.firmware.reason}
                  </span>
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
                    {node.map_report.nodes_online_local === 1
                      ? "node"
                      : "nodes"}{" "}
                    locally in 2 hours
                  </span>
                  <span className="data-line muted">
                    <Age now={now} at={node.map_report.received_at} />
                  </span>
                </>
              )
            }
          />
        </FactGroup>
      </div>
      <NodePositionMinimap
        node={node}
        links={links}
        linkView={linkView}
        radios={radios?.shared === true ? radios : null}
      />
    </div>
  );
}

function FactGroup({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <section className="fact-group">
      <h3>
        {title}
        {note === undefined ? null : (
          <span className="heading-note">{note}</span>
        )}
      </h3>
      <dl>{children}</dl>
    </section>
  );
}

/** Links to the numbers this node's radios reported under before or after it. */
function OtherNumbers({
  label,
  nodeIds,
}: {
  label: string;
  nodeIds: readonly number[];
}) {
  if (nodeIds.length === 0) return null;
  return (
    <span className="data-line muted">
      {label}{" "}
      {nodeIds.map((other, index) => (
        <span key={other}>
          {index === 0 ? null : ", "}
          <a className="text-button mono" href={nodeHref(other)}>
            {formatNodeId(other)}
          </a>
        </span>
      ))}
    </span>
  );
}

/** The node's current position: reported, from its map report, or estimated. */
function PositionItems({ node, now }: { node: NodeDetail; now: number }) {
  if (node.estimate !== null) {
    const { estimate } = node;
    return (
      <>
        <DataItem
          label="Coordinates"
          value={
            <span className="mono">
              ≈ {estimate.latitude.toFixed(3)}, {estimate.longitude.toFixed(3)}
            </span>
          }
        />
        <DataItem label="Source" value={estimateText(estimate)} />
      </>
    );
  }
  if (node.position === null) {
    return <DataItem label="Coordinates" value="Not reported" />;
  }
  const { position } = node;
  if (position.from_map_report) {
    return (
      <>
        <DataItem
          label="Coordinates"
          value={
            <span className="mono">
              ≈ {position.latitude.toFixed(3)}, {position.longitude.toFixed(3)}
            </span>
          }
        />
        <DataItem
          label="Source"
          value={mapReportPositionText(position.precision_km)}
        />
        <DataItem
          label="Updated"
          value={<Age now={now} at={position.observed_at} />}
        />
      </>
    );
  }
  return (
    <>
      <DataItem
        label="Coordinates"
        value={
          <span className="mono">
            {position.latitude.toFixed(5)}, {position.longitude.toFixed(5)}
          </span>
        }
      />
      {position.altitude_meters == null ? null : (
        <DataItem label="Altitude" value={`${position.altitude_meters} m`} />
      )}
      {node.location_source === null ? null : (
        <DataItem
          label="Source"
          value={LOCATION_SOURCES[node.location_source] ?? node.location_source}
        />
      )}
      <DataItem
        label="Updated"
        value={
          <Age
            now={now}
            at={position.observed_at}
            packetRowId={position.packet_row_id}
          />
        }
      />
    </>
  );
}

// What a position packet's location_source says about where it came from.
const LOCATION_SOURCES: Record<string, string> = {
  LOC_MANUAL: "Set by hand",
  LOC_INTERNAL: "From the node's own GPS",
  LOC_EXTERNAL: "From an external GPS, such as a phone",
};

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
      title={`Uploads to MQTT · last ${formatHours(hours)} ago`}
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
