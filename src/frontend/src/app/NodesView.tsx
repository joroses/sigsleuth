import { MouseEvent, ReactNode, useEffect, useState } from "react";

import {
  NODE_PAGE_SIZE,
  NodeChannelList,
  NodeListItem,
  NodeListPage,
  NodeSort,
  nodeChannelsUrl,
  nodesUrl,
} from "./api";
import { POSITION_MAX_AGE_MS } from "./mapTimeFilter";
import {
  formatHardware,
  formatNodeId,
  formatRole,
  mapHref,
  nodeHref,
  shortFirmware,
} from "./nodeDisplay";
import { useSetting, useViewState } from "./remember";
import { useApi } from "./useApi";
import { ErrorPanel, Icon, ShortTime, StatusPanel, WideTable } from "./ui";

const SORTS: NodeSort[] = [
  "traceroute",
  "last_sent",
  "packets",
  "texts",
  "channel_utilization",
  "air_util_tx",
  "firmware",
  "firmware_oldest",
  "altitude",
  "altitude_lowest",
];
const SEARCH_DELAY_MS = 300;

/**
 * Every known node, 20 per page, filtered and sorted by the server: find one
 * node, or spot the busiest senders, text chatters or traceroute participants.
 */
export function NodesView() {
  const [search, setSearch] = useViewState("nodes.search", "");
  const [query, setQuery] = useViewState("nodes.query", "");
  const [channel, setChannel] = useSetting<string | null>(
    "nodes.channel",
    null,
    (value) => value === null || typeof value === "string",
  );
  const [sort, setSort] = useSetting<NodeSort>(
    "nodes.sort",
    "traceroute",
    (value) => SORTS.includes(value as NodeSort),
  );
  const [gatewayOnly, setGatewayOnly] = useSetting("nodes.gatewayOnly", false);
  const [offset, setOffset] = useViewState("nodes.offset", 0);

  // Search once typing pauses, from the first page.
  useEffect(() => {
    if (search === query) return;
    const timer = window.setTimeout(() => {
      setQuery(search);
      setOffset(0);
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [search, query, setQuery, setOffset]);

  const { data, error } = useApi<NodeListPage>(
    nodesUrl({ query, channel, gatewayOnly, sort, offset }),
  );
  // Back can bring back a page past the end when the filters have changed
  // since; show the first page then.
  useEffect(() => {
    if (data !== null && data.items.length === 0 && offset > 0) setOffset(0);
  }, [data, offset, setOffset]);
  // Keep the previous page on screen while the next one loads.
  const [lastPage, setLastPage] = useState<NodeListPage | null>(null);
  if (data !== null && data !== lastPage) setLastPage(data);
  const page = data ?? lastPage;
  // Position ages only decide a colour; the time the page opened is close enough.
  const [now] = useState(Date.now);
  const { data: channels } = useApi<NodeChannelList>(nodeChannelsUrl);

  function chooseChannel(value: string) {
    const next = value === "" ? null : value;
    setChannel(next);
    setOffset(0);
  }

  function chooseSort(next: NodeSort) {
    setSort(next);
    setOffset(0);
  }

  const channelList = channels?.channels ?? [];
  return (
    <section className="nodes-page" aria-labelledby="nodes-heading">
      <div className="section-intro">
        <div>
          <h2 id="nodes-heading">Nodes</h2>
        </div>
      </div>
      <div className="nodes-toolbar">
        <input
          type="search"
          aria-label="Search nodes"
          placeholder="Search name, !hex or number"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          aria-label="Channel"
          value={channel ?? ""}
          onChange={(event) => chooseChannel(event.target.value)}
        >
          <option value="">All channels</option>
          {channelList.map((item) => (
            <option key={item.channel} value={item.channel}>
              {item.channel} ({item.nodes.toLocaleString()})
            </option>
          ))}
          {channel !== null &&
          !channelList.some((item) => item.channel === channel) ? (
            <option value={channel}>{channel}</option>
          ) : null}
        </select>
        <label>
          <input
            type="checkbox"
            checked={gatewayOnly}
            onChange={(event) => {
              setGatewayOnly(event.target.checked);
              setOffset(0);
            }}
          />
          Gateways only
        </label>
      </div>
      {error !== null ? <ErrorPanel message={error} /> : null}
      {page === null ? (
        error === null ? (
          <StatusPanel>Loading nodes…</StatusPanel>
        ) : null
      ) : page.items.length === 0 ? (
        <StatusPanel>No nodes match.</StatusPanel>
      ) : (
        <>
          <NodeTable
            nodes={page.items}
            sort={sort}
            onSort={chooseSort}
            loading={data === null}
            now={now}
          />
          <Paging
            offset={offset}
            shown={page.items.length}
            total={page.total}
            onOffset={setOffset}
          />
        </>
      )}
    </section>
  );
}

function NodeTable({
  nodes,
  sort,
  onSort,
  loading,
  now,
}: {
  nodes: NodeListItem[];
  sort: NodeSort;
  onSort: (sort: NodeSort) => void;
  loading: boolean;
  now: number;
}) {
  return (
    <WideTable className={loading ? "is-loading" : ""}>
      <table className="packet-table node-table">
        <thead>
          <tr>
            <th scope="col">Node ID</th>
            <th scope="col">Short</th>
            <th scope="col">Long name</th>
            {/* Map sorts by altitude, highest first; clicking it again turns it round. */}
            <SortHeading
              label="Map"
              active={sort === "altitude" || sort === "altitude_lowest"}
              ascending={sort === "altitude_lowest"}
              onClick={() =>
                onSort(sort === "altitude" ? "altitude_lowest" : "altitude")
              }
            >
              <span className="heading-note">altitude</span>
            </SortHeading>
            <th scope="col">Channel</th>
            <th scope="col" className="wide-only">
              Role
            </th>
            <th scope="col">Gateway</th>
            <SortHeading
              label="Last sent"
              active={sort === "last_sent"}
              onClick={() => onSort("last_sent")}
            />
            <SortHeading
              label="Traceroute"
              title="Distinct traceroute hops involving this node"
              active={sort === "traceroute"}
              onClick={() => onSort("traceroute")}
            >
              <span className="heading-note">hops · 24 h</span>
            </SortHeading>
            <SortHeading
              label="Sent"
              title="Packets this node sent in the last 24 hours"
              active={sort === "packets"}
              onClick={() => onSort("packets")}
            >
              <span className="heading-note">24 h</span>
            </SortHeading>
            <SortHeading
              label="Texts"
              title="Texts this node sent to everyone in the last 24 hours"
              active={sort === "texts"}
              onClick={() => onSort("texts")}
            >
              <span className="heading-note">24 h</span>
            </SortHeading>
            <SortHeading
              label="ChUtil"
              title="How busy the channel is, as the node hears it"
              className="wide-only"
              active={sort === "channel_utilization"}
              onClick={() => onSort("channel_utilization")}
            >
              <span className="heading-note">%</span>
            </SortHeading>
            <SortHeading
              label="AirUtil"
              title="Share of the last hour spent transmitting"
              className="wide-only"
              active={sort === "air_util_tx"}
              onClick={() => onSort("air_util_tx")}
            >
              <span className="heading-note">%</span>
            </SortHeading>
            {/* Firmware sorts newest first; clicking it again turns it round. */}
            <SortHeading
              label="Firmware"
              title="Firmware, or the minimum it must run"
              className="wide-only"
              active={sort === "firmware" || sort === "firmware_oldest"}
              ascending={sort === "firmware_oldest"}
              onClick={() =>
                onSort(sort === "firmware" ? "firmware_oldest" : "firmware")
              }
            />
            <th scope="col" className="wide-only">
              Hardware
            </th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((node) => (
            <NodeRow key={node.node_id} node={node} now={now} />
          ))}
        </tbody>
      </table>
    </WideTable>
  );
}

function SortHeading({
  label,
  title,
  className,
  active,
  ascending = false,
  onClick,
  children,
}: {
  label: string;
  title?: string;
  className?: string;
  active: boolean;
  ascending?: boolean;
  onClick: () => void;
  children?: ReactNode;
}) {
  return (
    <th
      scope="col"
      className={className}
      aria-sort={active ? (ascending ? "ascending" : "descending") : undefined}
      title={title}
    >
      <button
        type="button"
        className={`sort-button ${active ? "is-active" : ""}`}
        onClick={onClick}
      >
        {label}{" "}
        <span className="sort-arrow" aria-hidden="true">
          {ascending ? "↑" : "↓"}
        </span>
      </button>
      {children}
    </th>
  );
}

function NodeRow({ node, now }: { node: NodeListItem; now: number }) {
  const href = nodeHref(node.node_id);
  function openRow(event: MouseEvent) {
    // Links in the row, and selecting text to copy it, keep their own behaviour.
    if (
      (event.target as Element).closest("a") !== null ||
      window.getSelection()?.toString()
    ) {
      return;
    }
    window.location.hash = href;
  }
  return (
    <tr className="clickable-row" onClick={openRow}>
      <th scope="row">
        <a
          className="text-button mono node-open"
          href={href}
          title="Node details"
        >
          {formatNodeId(node.node_id)}
          <Icon name="open" />
        </a>
      </th>
      <td>{node.short_name}</td>
      <td className="node-long-name" title={node.long_name ?? undefined}>
        {node.long_name}
      </td>
      <td>
        <MapCell node={node} now={now} />
      </td>
      <td>{node.channel}</td>
      <td className="wide-only">{node.role && formatRole(node.role)}</td>
      <td>
        {node.last_gateway_at !== null ? (
          <span
            className="gateway-badge"
            title={`Gateway, last reported a packet ${new Date(node.last_gateway_at).toLocaleString()}`}
            aria-label="Gateway"
          >
            <Icon name="radio" />
          </span>
        ) : null}
      </td>
      <td>
        {node.last_sent_at === null ? (
          <span className="muted" title="No own packet observed">
            none
          </span>
        ) : node.last_sent_packet_row_id === null ? (
          <ShortTime value={node.last_sent_at} />
        ) : (
          <a
            className="text-button"
            href={`#packets/${node.last_sent_packet_row_id}`}
          >
            <ShortTime value={node.last_sent_at} />
          </a>
        )}
      </td>
      <td className="number">
        {node.traceroute_hops > 0 ? (
          <span
            title={`${node.traceroute_hops.toLocaleString()} hops with ${node.traceroute_neighbours.toLocaleString()} nodes, last 24 h`}
          >
            {node.traceroute_neighbours.toLocaleString()}
          </span>
        ) : null}
      </td>
      <td className="number">
        {node.packets > 0 ? node.packets.toLocaleString() : null}
      </td>
      <td className="number">
        {node.broadcast_texts > 0
          ? node.broadcast_texts.toLocaleString()
          : null}
      </td>
      <td className="wide-only number">
        <Utilization node={node} value={node.channel_utilization} />
      </td>
      <td className="wide-only number">
        <Utilization node={node} value={node.air_util_tx} />
      </td>
      {node.firmware_version !== null ? (
        <td
          className="wide-only"
          title={`${node.firmware_version}, from its map report`}
        >
          {shortFirmware(node.firmware_version)}
        </td>
      ) : (
        <td
          className="wide-only"
          title={
            node.firmware
              ? `${node.firmware.minimum} or newer: ${node.firmware.reason}`
              : undefined
          }
        >
          {node.firmware && `≥ ${node.firmware.minimum}`}
        </td>
      )}
      <td
        className="wide-only node-hardware"
        title={
          node.hardware_model ? formatHardware(node.hardware_model) : undefined
        }
      >
        {node.hardware_model && formatHardware(node.hardware_model)}
      </td>
    </tr>
  );
}

/** One ChUtil or AirUtil percentage, with the telemetry's time on hover. */
function Utilization({
  node,
  value,
}: {
  node: NodeListItem;
  value: number | null;
}) {
  if (value === null || node.utilization_observed_at === null) return null;
  return (
    <span
      title={`Reported ${new Date(node.utilization_observed_at).toLocaleString()}`}
    >
      {value.toFixed(1)}%
    </span>
  );
}

function MapCell({ node, now }: { node: NodeListItem; now: number }) {
  if (node.position_observed_at === null) {
    return (
      <span
        className="map-pin map-pin-none"
        title="No position reported"
        aria-label="No position reported"
      >
        ○
      </span>
    );
  }
  const reported = new Date(node.position_observed_at);
  const old = now - reported.getTime() > POSITION_MAX_AGE_MS;
  const title = `Show on map; position from ${reported.toLocaleString()}${
    old ? ", older than 14 days" : ""
  }`;
  return (
    <span className="map-cell">
      <a
        className={`map-pin ${old ? "map-pin-old" : ""}`}
        href={mapHref(node.node_id)}
        title={title}
        aria-label={title}
      >
        <Icon name="pin" />
      </a>
      {node.altitude_meters === null ? null : `${node.altitude_meters} m`}
    </span>
  );
}

function Paging({
  offset,
  shown,
  total,
  onOffset,
}: {
  offset: number;
  shown: number;
  total: number;
  onOffset: (offset: number) => void;
}) {
  return (
    <div className="nodes-paging">
      <button
        type="button"
        className="secondary-button"
        disabled={offset === 0}
        onClick={() => onOffset(Math.max(0, offset - NODE_PAGE_SIZE))}
      >
        Previous
      </button>
      <span>
        {(offset + 1).toLocaleString()}–{(offset + shown).toLocaleString()} of{" "}
        {total.toLocaleString()}
      </span>
      <button
        type="button"
        className="secondary-button"
        disabled={offset + shown >= total}
        onClick={() => onOffset(offset + NODE_PAGE_SIZE)}
      >
        Next
      </button>
    </div>
  );
}
