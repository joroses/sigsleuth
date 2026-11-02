import {
  ReactNode,
  Suspense,
  lazy,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  DiagnosticsSummary,
  DeviceConflictFilter,
  DeviceConflictList,
  DoubtfulNodeList,
  LongLink,
  LongLinkEnd,
  LongLinkFilter,
  LongLinkOrder,
  LongLinkList,
  MapNode,
  MapNodeList,
  PacketPortList,
  diagnosticsSummaryUrl,
  deviceConflictsUrl,
  doubtfulNodesUrl,
  longestLinksUrl,
  mapNodesUrl,
  packetPortsUrl,
  packetsUrl,
  request,
} from "./api";
import {
  diagnosticsLinksHref,
  diagnosticsPacketsHref,
  formatKm,
  formatNodeId,
  nodeLabel,
  nodeName,
  relayChecksHref,
} from "./nodeDisplay";
import { LongLinkItem, ShownBadge } from "./LongLinks";
import { NodeSearch } from "./NodeSearch";
import { PagedPacketTable, PortSelect } from "./PacketTable";
import {
  readView,
  saveSetting,
  useSetting,
  useViewState,
  writeView,
} from "./remember";
import { ApiState, openedAt, useApi } from "./useApi";
import {
  Badge,
  DIAGNOSTICS_LINKED,
  DataItem,
  DoubtfulBadge,
  Icon,
  IconButton,
  IconName,
  NodeLink,
  OpenedAt,
  ShortTime,
  StatusPanel,
  WideTable,
} from "./ui";

export type DiagnosticsTab =
  "overview" | "links" | "doubtful" | "devices" | "relays" | "packets";

const TABS: {
  tab: DiagnosticsTab;
  icon: IconName;
  label: string;
  href: string;
}[] = [
  {
    tab: "overview",
    icon: "diagnostics",
    label: "Overview",
    href: "#diagnostics",
  },
  {
    tab: "links",
    icon: "links",
    label: "Long links",
    href: diagnosticsLinksHref(),
  },
  {
    tab: "doubtful",
    icon: "locate",
    label: "Positions to check",
    href: "#diagnostics/doubtful",
  },
  {
    tab: "devices",
    icon: "nodes",
    label: "ID conflicts",
    href: "#diagnostics/devices",
  },
  {
    tab: "relays",
    icon: "route",
    label: "Relay checks",
    href: relayChecksHref(),
  },
  {
    tab: "packets",
    icon: "table",
    label: "Packets",
    href: diagnosticsPacketsHref(null),
  },
];

// Relay checks bring in MapLibre, so they load only on their own tab.
const RelayChecks = lazy(() =>
  import("./RelayChecks").then((module) => ({ default: module.RelayChecks })),
);

export function Diagnostics({
  tab,
  node: linkNode = null,
  sender = null,
  heardDirectlyBy = null,
  receptionId = null,
  surveys = false,
  measurementId = null,
}: {
  tab: DiagnosticsTab;
  /** The long links listed: only this node's (#diagnostics/links/node). */
  node?: number | null;
  /** The packets listed: only those sender sent, and heardDirectlyBy heard
   * with no relay (#diagnostics/packets/sender-gateway). */
  sender?: number | null;
  heardDirectlyBy?: number | null;
  /** The relay check open (#diagnostics/relays/reception). */
  receptionId?: number | null;
  /** Survey uploads shown, and the measurement open
   * (#diagnostics/relays/surveys/measurement). */
  surveys?: boolean;
  measurementId?: number | null;
}) {
  const summary = useApi<DiagnosticsSummary>(diagnosticsSummaryUrl);
  // Loaded, so the password was accepted (or none is set): link the page in
  // this browser's bar from now on.
  const loaded = summary.data !== null;
  useEffect(() => {
    if (loaded) saveSetting(DIAGNOSTICS_LINKED, true);
  }, [loaded]);
  // A tab stays mounted, hidden, once opened, so going back to it shows what
  // it had loaded; a hidden one keeps the filters it was left with.
  const [opened, setOpened] = useState<DiagnosticsTab[]>([tab]);
  if (!opened.includes(tab)) setOpened([...opened, tab]);
  const node = useHeldWhile(tab !== "links", linkNode);
  const from = useHeldWhile(tab !== "packets", sender);
  const heardBy = useHeldWhile(tab !== "packets", heardDirectlyBy);
  const reception = useHeldWhile(tab !== "relays", receptionId);
  const surveysShown = useHeldWhile(tab !== "relays", surveys);
  const measurement = useHeldWhile(tab !== "relays", measurementId);
  const nodes = useNodes(node !== null || from !== null);
  const panel = (which: DiagnosticsTab, content: ReactNode) =>
    opened.includes(which) ? <div hidden={tab !== which}>{content}</div> : null;
  return (
    <div className="diagnostics-page">
      <nav className="page-tabs" aria-label="Diagnostics views">
        {TABS.map((item) => (
          <a
            key={item.tab}
            href={item.href}
            aria-current={item.tab === tab ? "page" : undefined}
          >
            <Icon name={item.icon} />
            <strong>{item.label}</strong>
          </a>
        ))}
      </nav>
      {panel(
        "overview",
        <>
          <SummarySection state={summary} />
          <SkippedSection state={summary} />
        </>,
      )}
      {panel("doubtful", <DoubtfulNodesSection />)}
      {panel("devices", <DeviceConflictsSection />)}
      {panel("links", <LongestLinksSection node={node} nodes={nodes} />)}
      {panel(
        "relays",
        <Suspense fallback={<StatusPanel>Loading…</StatusPanel>}>
          <RelayChecks
            receptionId={reception}
            surveys={surveysShown}
            measurementId={measurement}
          />
        </Suspense>,
      )}
      {panel(
        "packets",
        <PacketsSection
          sender={from}
          heardDirectlyBy={heardBy}
          nodes={nodes}
        />,
      )}
    </div>
  );
}

/**
 * `value`, or while `hold` the one it had when hold began: a hidden tab
 * keeps its filters, rather than loading the unfiltered list unseen.
 */
function useHeldWhile<T>(hold: boolean, value: T): T {
  const [held, setHeld] = useState(value);
  if (!hold && held !== value) setHeld(value);
  return hold ? held : value;
}

/**
 * useViewState for a page that stays open while its own address changes
 * (#diagnostics/…): a new history entry starts with the value as it
 * is, and Back to an entry brings back its value.
 */
function usePageView<T>(key: string, initial: T) {
  const [value, setValue] = useViewState<T>(key, initial);
  const current = useRef(value);
  useEffect(() => {
    current.current = value;
  }, [value]);
  useEffect(() => {
    const follow = () => {
      const saved = readView(key);
      if (saved === undefined) writeView(key, current.current);
      else setValue(saved as T);
    };
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, [key, setValue]);
  return [value, setValue] as const;
}

/** The map's nodes, for the node searches and the chosen nodes' names. */
interface NodeChoices {
  nodes: MapNode[];
  name: (nodeId: number) => string;
  /** Loads the nodes; a search calls it when it gets focus. */
  want: () => void;
}

// Loaded only once a node filter is set or a search is used: the page is
// usually opened for the rest.
function useNodes(needed: boolean): NodeChoices {
  const [wanted, setWanted] = useState(false);
  const state = useApi<MapNodeList>(needed || wanted ? mapNodesUrl({}) : null);
  const items = state.data?.items;
  return useMemo(() => {
    const nodes = items ?? [];
    const names = new Map(nodes.map((node) => [node.node_id, nodeLabel(node)]));
    return {
      nodes,
      name: (nodeId) => names.get(nodeId) ?? formatNodeId(nodeId),
      want: () => setWanted(true),
    };
  }, [items]);
}

/** A node chosen to filter by, with a button to clear it, or a search to choose one. */
function NodeFilter({
  label,
  nodeId,
  nodes,
  onPick,
}: {
  label: string;
  nodeId: number | null;
  nodes: NodeChoices;
  onPick: (nodeId: number | null) => void;
}) {
  return (
    <div className="diagnostic-node-filter" onFocus={nodes.want}>
      <span className="diagnostic-node-filter-label">{label}</span>
      {nodeId === null ? (
        <NodeSearch
          nodes={nodes.nodes}
          label={label}
          onSelect={(node) => onPick(node.node_id)}
          onSelectId={onPick}
        />
      ) : (
        <span className="los-chosen">
          <NodeLink nodeId={nodeId} name={nodes.name(nodeId)} />
          <IconButton
            icon="close"
            label={`Clear ${label.toLowerCase()}`}
            onClick={() => onPick(null)}
          />
        </span>
      )}
    </div>
  );
}

function SummarySection({ state }: { state: ApiState<DiagnosticsSummary> }) {
  if (state.error !== null) {
    return <StatusPanel tone="error">{state.error}</StatusPanel>;
  }
  if (state.data === null) {
    return <StatusPanel>Loading service and database facts…</StatusPanel>;
  }
  const summary = state.data;
  const database = summary.database;
  return (
    <section aria-label="Overview">
      <div className="diagnostic-facts">
        <dl>
          <DataItem
            label="MQTT capture"
            value={
              summary.capture?.status === "connected" ? (
                <Badge tone="green">connected</Badge>
              ) : (
                <Badge tone="amber">
                  {summary.capture === null
                    ? "Not running"
                    : (summary.capture.stopped_reason ??
                      summary.capture.status)}
                </Badge>
              )
            }
          />
          <DataItem
            label="Last MQTT message"
            value={formatOptionalTime(summary.capture?.last_message_at ?? null)}
          />
          <DataItem
            label="Retention last ran"
            value={formatOptionalTime(
              summary.capture?.retention_ran_at ?? null,
            )}
          />
          <DataItem
            label="API uptime"
            value={formatDuration(summary.api_uptime_seconds)}
          />
          <DataItem
            label="Commit"
            value={
              summary.revision === null ? (
                "Unknown"
              ) : (
                <a
                  href={`https://github.com/joroses/sigsleuth/commit/${summary.revision}`}
                >
                  {summary.revision.slice(0, 7)}
                </a>
              )
            }
          />
        </dl>
        <dl>
          <DataItem
            label="Facts version"
            value={String(database.facts_version)}
          />
          <DataItem
            label="Age limit"
            value={
              database.retention_days === 0
                ? "None"
                : `${database.retention_days} days`
            }
          />
          <DataItem
            label="Database / WAL"
            value={`${formatBytes(database.database_size_bytes)} / ${formatBytes(database.wal_size_bytes)}`}
          />
          <DataItem
            label="Oldest saved packet"
            value={formatOptionalTime(database.oldest_packet_at)}
          />
          <DataItem
            label="Latest saved packet"
            value={formatOptionalTime(database.newest_packet_at)}
          />
        </dl>
        <dl>
          {Object.entries(database.row_counts).map(([table, count]) => (
            <DataItem
              key={table}
              label={table}
              value={count.toLocaleString()}
            />
          ))}
        </dl>
      </div>
    </section>
  );
}

function SkippedSection({ state }: { state: ApiState<DiagnosticsSummary> }) {
  if (state.data === null) {
    return null;
  }
  const { capture, skipped } = state.data;
  return (
    <section aria-labelledby="diagnostic-skipped-heading">
      <div className="section-intro compact">
        <h3 id="diagnostic-skipped-heading">Skipped deliveries</h3>
        <p>
          Messages from MQTT that SigSleuth threw away instead of saving,
          counted by reason since capture last started. They were never saved,
          so they don&apos;t appear anywhere else.
        </p>
      </div>
      {capture === null ? (
        <p className="empty-inline">Capture is not running.</p>
      ) : skipped.length === 0 ? (
        <p className="empty-inline">No deliveries skipped yet.</p>
      ) : (
        <div className="table-wrap diagnostic-skipped">
          <table>
            <thead>
              <tr>
                <th scope="col">Reason</th>
                <th scope="col">Deliveries</th>
                <th scope="col">What it means</th>
              </tr>
            </thead>
            <tbody>
              {skipped.map((skip) => (
                <tr key={skip.reason}>
                  <th scope="row">
                    <code>{skip.reason}</code>
                  </th>
                  <td>{skip.count}</td>
                  <td>{skip.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function PacketsSection({
  sender,
  heardDirectlyBy,
  nodes,
}: {
  sender: number | null;
  heardDirectlyBy: number | null;
  nodes: NodeChoices;
}) {
  const [port, setPort] = usePageView<number | null>(
    "diagnostics.packetPort",
    null,
  );
  const ports = useApi<PacketPortList>(packetPortsUrl);
  // The node filters are in the address, so Back undoes them and a long
  // link can link to its packets.
  const filterBy = (nextSender: number | null, gateway: number | null) => {
    window.location.hash = diagnosticsPacketsHref(nextSender, gateway);
  };

  return (
    <section aria-label="Packets">
      <div className="section-intro compact">
        <p>
          Every saved packet, newest first. To see what a gateway hears straight
          from a node, with no relay in between, pick the node under{" "}
          <strong>From</strong>, then the gateway under{" "}
          <strong>Heard directly by</strong>.
        </p>
      </div>
      <div className="nodes-toolbar">
        <PortSelect
          ports={ports.data?.ports ?? []}
          port={port}
          onChange={setPort}
        />
        <NodeFilter
          label="From"
          nodeId={sender}
          nodes={nodes}
          onPick={(picked) => filterBy(picked, null)}
        />
        {sender !== null ? (
          <NodeFilter
            label="Heard directly by"
            nodeId={heardDirectlyBy}
            nodes={nodes}
            onPick={(picked) => filterBy(sender, picked)}
          />
        ) : null}
      </div>
      <PagedPacketTable
        url={packetsUrl({ port, sender, heardDirectlyBy })}
        empty={
          port === null && sender === null
            ? "No packets are saved yet."
            : "No saved packets match these filters."
        }
      />
    </section>
  );
}

const LINK_FILTERS: { value: LongLinkFilter | "all"; label: string }[] = [
  { value: "all", label: "All links" },
  { value: "long", label: "Still shown long" },
  { value: "shorter", label: "Now shorter" },
  { value: "hidden", label: "Hidden" },
  { value: "shared", label: "Shared node number" },
  { value: "position", label: "Doubtful position" },
  { value: "unlikely", label: "Unlikely by terrain" },
  { value: "signal", label: "Signal mismatch" },
  { value: "unconfirmed", label: "Unconfirmed" },
  { value: "checks", label: "With checks to review" },
];

const LINK_ORDERS: { value: LongLinkOrder; label: string }[] = [
  { value: "longest", label: "Longest first" },
  { value: "latest", label: "Recent first" },
];

// How many links a page looks through: a filtered one more, since few
// match. MAX_LONG_LINK_SCAN in diagnostics.py caps a request.
const LINK_PAGE = 30;
const FILTERED_LINK_PAGE = 300;
const MAX_LINK_REQUEST = 1000;

function LongestLinksSection({
  node,
  nodes,
}: {
  node: number | null;
  nodes: NodeChoices;
}) {
  const [shown, setShown] = useSetting<LongLinkFilter | "all">(
    "diagnostics.longLinks",
    "all",
    (value) => LINK_FILTERS.some((item) => item.value === value),
  );
  const [order, setOrder] = useSetting<LongLinkOrder>(
    "diagnostics.longLinksOrder",
    "longest",
    (value) => LINK_ORDERS.some((item) => item.value === value),
  );
  const only = shown === "all" ? null : shown;
  const state = useLongLinks(only, node, order);
  // The links opened in the list, by pair, kept for Back.
  const [opened, setOpened] = usePageView<string[]>(
    "diagnostics.openLinks",
    [],
  );
  const counts = { long: 0, shorter: 0, hidden: 0 };
  for (const item of state.data?.items ?? []) counts[item.shown] += 1;
  const data = state.data;
  return (
    <section aria-label="Long links">
      <div className="section-intro compact">
        <p>
          The longest distances between two nodes that heard each other. A long
          link can be real, or a sign that one end&apos;s position is wrong.
          Each one shows whether the map still draws it, now draws a shorter,
          newer distance, or hides it. Open a link to see why, and what to check
          to decide whether it&apos;s real.
        </p>
      </div>
      <div className="nodes-toolbar">
        <select
          aria-label="Show"
          value={shown}
          onChange={(event) =>
            setShown(event.target.value as LongLinkFilter | "all")
          }
        >
          {LINK_FILTERS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Order"
          value={order}
          onChange={(event) => setOrder(event.target.value as LongLinkOrder)}
        >
          {LINK_ORDERS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        {/* In the address, so Back undoes it and other tabs link to it. */}
        <NodeFilter
          label="Node"
          nodeId={node}
          nodes={nodes}
          onPick={(picked) => {
            window.location.hash = diagnosticsLinksHref(picked);
          }}
        />
        <OpenedAt at={state.openedAt} onRefresh={state.refresh} />
      </div>
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : data === null ? (
        <StatusPanel>Loading long links…</StatusPanel>
      ) : (
        <>
          {only === null && data.items.length > 0 ? (
            <p className="longest-links-counts">
              {(["long", "shorter", "hidden"] as const).map((kind) => (
                <ShownBadge
                  key={kind}
                  shown={kind}
                  rules={data.rules}
                  count={counts[kind]}
                />
              ))}
            </p>
          ) : null}
          {data.items.length > 0 ? (
            <ol className="longest-links">
              {data.items.map((item) => {
                const key = pairKey(item);
                return (
                  <LongLinkItem
                    key={key}
                    link={item}
                    rules={data.rules}
                    latest={order === "latest"}
                    open={opened.includes(key)}
                    onToggle={(open) =>
                      setOpened((keys) =>
                        open
                          ? [...keys.filter((other) => other !== key), key]
                          : keys.filter((other) => other !== key),
                      )
                    }
                  />
                );
              })}
            </ol>
          ) : null}
          <p className="end-note">
            {lookedThrough(data, only, node, nodes, order)}
          </p>
          {state.loadMoreError !== null ? (
            <StatusPanel tone="error">{state.loadMoreError}</StatusPanel>
          ) : null}
          {data.next_offset !== null ? (
            <button
              type="button"
              className="secondary-button page-button"
              disabled={state.loadingMore}
              onClick={state.loadMore}
            >
              {state.loadingMore
                ? "Loading…"
                : only === null
                  ? `Show ${LINK_PAGE} more`
                  : `Look through ${FILTERED_LINK_PAGE} more`}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}

function pairKey(link: LongLink): string {
  return `${link.node_a.node.node_id}-${link.node_b.node.node_id}`;
}

function lookedThrough(
  data: LongLinkList,
  only: LongLinkFilter | null,
  node: number | null,
  nodes: NodeChoices,
  order: LongLinkOrder,
): ReactNode {
  const latest = order === "latest";
  if (data.looked_through === 0) {
    const what = latest
      ? "link heard 250 km or more apart"
      : "link with a distance";
    return node === null
      ? `No ${what} yet.`
      : `${nodes.name(node)} has no ${what}.`;
  }
  const whose = node === null ? "" : ` of ${nodes.name(node)}`;
  const links = `${data.looked_through.toLocaleString()} ${latest ? "latest links heard 250 km+ apart" : "longest links"}${whose}`;
  const depth =
    data.next_offset === null ? (
      "all of them"
    ) : latest && data.oldest_hour_at !== null ? (
      <>
        back to <ShortTime value={data.oldest_hour_at} />
      </>
    ) : (
      `down to ${formatKm(data.lowest_km)} km`
    );
  if (only === null) {
    return (
      <>
        The {links}, {depth}.
      </>
    );
  }
  const matched = data.items.length;
  return (
    <>
      {matched.toLocaleString()} of the {links} match, {depth}.
    </>
  );
}

/**
 * A page of long links that grows with loadMore. The first request looks as
 * far down as this history entry had, so Back shows the same links again,
 * and is kept under when the list was opened (openedAt), so a visit soon
 * after shows it without loading it.
 */
function useLongLinks(
  only: LongLinkFilter | null,
  node: number | null,
  order: LongLinkOrder,
) {
  const step = only === null ? LINK_PAGE : FILTERED_LINK_PAGE;
  const key = `${only}/${node}/${order}`;
  const [looked, setLooked] = usePageView<{
    key: string;
    looked: number;
  } | null>(LOOKED, null);
  const [restored, setRestored] = useState(looked);
  const count =
    restored?.key === key
      ? Math.min(MAX_LINK_REQUEST, Math.max(step, restored.looked))
      : step;
  const [listedAt, setListedAt] = useState(() => openedAt(LONG_LINKS_VIEW));
  const first = useApi<LongLinkList>(
    longestLinksUrl({ offset: 0, count, only, node, order }),
    listedAt,
  );
  // Later pages, valid only while the first page they extend is shown.
  const [more, setMore] = useState<{
    first: LongLinkList;
    data: LongLinkList;
    loading: boolean;
    error: string | null;
  } | null>(null);
  const extra = more !== null && more.first === first.data ? more : null;
  const data = extra?.data ?? first.data;
  useEffect(() => {
    if (data !== null) setLooked({ key, looked: data.looked_through });
  }, [key, data, setLooked]);
  const loading = useRef<AbortController | null>(null);
  useEffect(() => () => loading.current?.abort(), [key]);
  useEffect(() => {
    const follow = () => {
      loading.current?.abort();
      setMore(null);
      setRestored((readView(LOOKED) as typeof looked) ?? null);
    };
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, []);

  function loadMore() {
    const base = first.data;
    if (base === null || data === null || data.next_offset === null) return;
    if (extra?.loading) return;
    loading.current?.abort();
    const controller = new AbortController();
    loading.current = controller;
    setMore({ first: base, data, loading: true, error: null });
    const url = longestLinksUrl({
      offset: data.next_offset,
      count: step,
      only,
      node,
      order,
    });
    request<LongLinkList>(url, controller.signal).then(
      (next) =>
        setMore({
          first: base,
          data: { ...next, items: [...data.items, ...next.items] },
          loading: false,
          error: null,
        }),
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setMore({
          first: base,
          data,
          loading: false,
          error:
            error instanceof Error
              ? error.message
              : "Could not load more links.",
        });
      },
    );
  }

  return {
    data,
    error: first.error,
    loadingMore: extra?.loading ?? false,
    loadMoreError: extra?.error ?? null,
    loadMore,
    openedAt: listedAt,
    refresh: () => setListedAt(openedAt(LONG_LINKS_VIEW, true)),
  };
}

const LOOKED = "diagnostics.longLinksLooked";
const LONG_LINKS_VIEW = "diagnostics.longLinks";

/**
 * Every doubtful node, and the nodes the terrain check most
 * often blames, with their long-link counts.
 */
function DoubtfulNodesSection() {
  const state = useApi<DoubtfulNodeList>(doubtfulNodesUrl);
  return (
    <section aria-label="Positions to check">
      <div className="section-intro compact">
        <p>
          Nodes that may not be where the map puts them. A node is listed when
          the nodes that hear it directly are mostly too far away, when its
          altitude says it was flying, or when two or more of its long links are
          unlikely over the terrain. The map hides links from nodes marked
          doubtful.
        </p>
      </div>
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : state.data === null ? (
        <StatusPanel>Loading positions to check…</StatusPanel>
      ) : state.data.items.length === 0 ? (
        <p className="empty-inline">No position to check.</p>
      ) : (
        <WideTable>
          <table>
            <EndHeadings>
              <th scope="col">Links 250 km+</th>
            </EndHeadings>
            <tbody>
              {state.data.items.map((item) => (
                <EndRow key={item.end.node.node_id} end={item.end}>
                  <td>
                    {item.long_links === 0
                      ? "none"
                      : `${item.long_links}, ${item.unlikely_links} unlikely`}
                  </td>
                </EndRow>
              ))}
            </tbody>
          </table>
        </WideTable>
      )}
    </section>
  );
}

const DEVICE_CASES: Record<
  DeviceConflictFilter,
  { label: string; tone: "neutral" | "amber" | "blue" }
> = {
  shared: { label: "shared by two radios", tone: "amber" },
  one_at_a_time: { label: "new MAC", tone: "blue" },
  keys: { label: "new key only", tone: "neutral" },
};

const DEVICE_FILTERS = [
  { value: "all" as const, label: "IDs", tone: "neutral" as const },
  ...(Object.keys(DEVICE_CASES) as DeviceConflictFilter[]).map((value) => ({
    value,
    ...DEVICE_CASES[value],
  })),
];

function DeviceCaseBadge({ kind }: { kind: DeviceConflictFilter }) {
  const { label, tone } = DEVICE_CASES[kind];
  return (
    <Badge tone={tone}>
      {label.charAt(0).toUpperCase()}
      {label.slice(1)}
    </Badge>
  );
}

function DeviceConflictsSection() {
  const [shown, setShown] = useSetting<DeviceConflictFilter | "all">(
    "diagnostics.devices",
    "all",
    (value) => DEVICE_FILTERS.some((item) => item.value === value),
  );
  const [afterNode, setAfterNode] = useState(0);
  const state = useApi<DeviceConflictList>(
    deviceConflictsUrl(afterNode, shown === "all" ? null : shown),
  );
  const nextNode = state.data?.next_node_id;
  const [counts, setCounts] = useState<DeviceConflictList["counts"] | null>(
    null,
  );
  if (state.data !== null && counts !== state.data.counts) {
    setCounts(state.data.counts);
  }
  const total =
    counts === null
      ? 0
      : Object.values(counts).reduce((sum, count) => sum + count, 0);
  return (
    <section aria-label="ID conflicts">
      <div className="section-intro compact">
        <p>
          Node IDs that have reported more than one MAC address or public key.
          There are three cases:
        </p>
        <ul>
          <li>
            <strong>Shared by two radios:</strong> two radios use this ID at the
            same time, so SigSleuth can&apos;t tell them apart and hides their
            links.
          </li>
          <li>
            <strong>New MAC:</strong> the radio was probably replaced, or two
            radios used the ID but never at the same moment.
          </li>
          <li>
            <strong>New key only:</strong> the same radio got a new key, for
            example after a reset.
          </li>
        </ul>
      </div>
      {counts === null ? null : (
        <div
          className="longest-links-counts device-filters"
          role="group"
          aria-label="Show"
        >
          {DEVICE_FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`badge badge-${item.tone}`}
              aria-pressed={shown === item.value}
              onClick={() => {
                setShown(item.value);
                setAfterNode(0);
              }}
            >
              {item.value === "all" ? total : counts[item.value]} {item.label}
            </button>
          ))}
        </div>
      )}
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : state.data === null ? (
        <StatusPanel>Loading ID conflicts…</StatusPanel>
      ) : state.data.items.length === 0 ? (
        <p className="empty-inline">
          No node ID reports conflicting MACs or keys.
        </p>
      ) : (
        state.data.items.map((item) => (
          <div key={item.node_id}>
            <h4>
              <NodeLink nodeId={item.node_id} />{" "}
              <DeviceCaseBadge kind={item.kind ?? "keys"} />
            </h4>
            {item.proof === null ? null : (
              <SharedProofLine proof={item.proof} />
            )}
            <WideTable>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Reported name</th>
                    <th scope="col">Hardware</th>
                    <th scope="col">MAC address</th>
                    <th scope="col">Public key</th>
                    <th scope="col">First reported</th>
                    <th scope="col">Last reported</th>
                    <th scope="col">Same MAC under</th>
                  </tr>
                </thead>
                <tbody>
                  {item.reports.map((report) => (
                    <tr key={report.packet_row_id}>
                      <td>{report.long_name ?? report.short_name ?? "—"}</td>
                      <td>{report.hardware_model ?? "—"}</td>
                      <td className="mono">{report.mac_address ?? "—"}</td>
                      <td>
                        <code>{report.public_key ?? "—"}</code>
                      </td>
                      <td>
                        <ShortTime value={report.first_observed_at} />
                      </td>
                      <td>
                        <a href={`#packets/${report.packet_row_id}`}>
                          <ShortTime value={report.observed_at} />
                        </a>
                      </td>
                      <td>
                        {report.other_node_ids.length === 0
                          ? "—"
                          : report.other_node_ids.map((other, index) => (
                              <span key={other}>
                                {index === 0 ? null : ", "}
                                <NodeLink nodeId={other} />
                              </span>
                            ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </WideTable>
            {item.limited ? <p>Showing the newest 20 device reports.</p> : null}
          </div>
        ))
      )}
      <div className="button-row">
        {afterNode !== 0 ? (
          <button onClick={() => setAfterNode(0)}>First IDs</button>
        ) : null}
        {nextNode != null ? (
          <button onClick={() => setAfterNode(nextNode)}>Next IDs</button>
        ) : null}
      </div>
    </section>
  );
}

/** The packets that show a node number shared: a NodeInfo inside a run. */
function SharedProofLine({
  proof,
}: {
  proof: NonNullable<DeviceConflictList["items"][number]["proof"]>;
}) {
  const packet = (rowId: number | null, label: string) =>
    rowId === null ? (
      <span className="muted">{label}</span>
    ) : (
      <a href={`#packets/${rowId}`}>{label}</a>
    );
  return (
    <p>
      Last shown <ShortTime value={proof.proof_at} />:{" "}
      {packet(proof.other_packet_row_id, "NodeInfo")} inside the run{" "}
      {packet(proof.run_from_packet_row_id, "from")} –{" "}
      {packet(proof.run_to_packet_row_id, "to")}
    </p>
  );
}

/** The headings of a table of EndRows, with `children` before the last. */
function EndHeadings({ children }: { children?: ReactNode }) {
  return (
    <thead>
      <tr>
        <th scope="col">Node</th>
        <th scope="col">Position</th>
        <th scope="col">Altitude</th>
        <th scope="col">Moved</th>
        <th scope="col">Direct partners, 7 days</th>
        {children}
        <th scope="col">
          <span className="visually-hidden">Long links</span>
        </th>
      </tr>
    </thead>
  );
}

/** A node as a long link's end, with `children` cells before the link to its long links. */
function EndRow({ end, children }: { end: LongLinkEnd; children?: ReactNode }) {
  const node = end.node;
  return (
    <tr>
      <th scope="row">
        <NodeLink
          nodeId={node.node_id}
          name={nodeName(node)}
          title={formatNodeId(node.node_id)}
        />{" "}
        {end.doubtful ? (
          <DoubtfulBadge
            suspects={[nodeName(node) ?? formatNodeId(node.node_id)]}
          />
        ) : null}
      </th>
      <td>
        {node.latitude === null || node.longitude === null ? (
          "none"
        ) : (
          <>
            {node.latitude.toFixed(4)}, {node.longitude.toFixed(4)}
            {node.estimated ? (
              <span className="muted"> estimated</span>
            ) : end.position_observed_at !== null ? (
              <span className="muted">
                {" "}
                {end.position_from_map_report ? "map report " : ""}
                <ShortTime value={end.position_observed_at} />
              </span>
            ) : null}
          </>
        )}
      </td>
      <td>
        {end.altitude_meters === null
          ? "—"
          : end.altitude_meters === 0
            ? "0 m (not set)"
            : `${end.altitude_meters.toLocaleString()} m`}
      </td>
      <td>
        {end.moved_at === null ? "—" : <ShortTime value={end.moved_at} />}
      </td>
      <td>
        {end.direct_partners > 0
          ? `${end.direct_partners}: nearest ${formatKm(end.nearest_partner_km)} km, median ${formatKm(end.median_partner_km)} km (limit ${formatKm(end.doubt_limit_km)} km)`
          : end.blamed_partners === 0
            ? "none placed"
            : null}
        {end.blamed_partners > 0 ? (
          <div>
            {end.blamed_partners} far not counted: doubtful on their other
            partners
          </div>
        ) : null}
        {end.traceroute_corroborated ? (
          <div>Position corroborated by nearby traceroute hops</div>
        ) : null}
      </td>
      {children}
      <td>
        <a className="text-button" href={diagnosticsLinksHref(node.node_id)}>
          Its long links
        </a>
      </td>
    </tr>
  );
}

function formatOptionalTime(value: string | null): ReactNode {
  return value === null ? "None yet" : <ShortTime value={value} />;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) {
    return `${Math.floor(seconds)} seconds`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} minutes`;
  }
  return `${(seconds / 3600).toFixed(1)} hours`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KiB`;
  }
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
}
