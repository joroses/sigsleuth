import {
  type ReactNode,
  Suspense,
  lazy,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  NodeSummary,
  NodeCount,
  TracerouteFilters,
  TracerouteRun,
  TracerouteRuns,
  TracerouteSort,
  BusiestTraceroutes,
  TRACEROUTE_PAGE_SIZE,
  traceroutesUrl,
  busiestTraceroutesUrl,
} from "./api";
import { type DirectedHop, routeMinimap, zeroCostHops } from "./minimapMarkers";
import {
  doubtedNames,
  formatKm,
  formatNodeId,
  nodeName,
  packetHopHref,
} from "./nodeDisplay";
import { type NodeHighlight, RouteHopList, RouteItem } from "./RouteItem";
import { type MinimapFocus } from "./PacketMinimap";
import { useSetting, useViewState } from "./remember";
import { useApi } from "./useApi";
import {
  DoubtfulBadge,
  ErrorPanel,
  Icon,
  PendingBadge,
  ShortTime,
  SortHeading,
  StatusPanel,
  WideTable,
} from "./ui";

// The minimap brings in MapLibre, so it loads when a run is first opened.
const PacketMinimap = lazy(() =>
  import("./PacketMinimap").then((module) => ({
    default: module.PacketMinimap,
  })),
);

// The Links page's Traceroute explorer: the traceroute runs in the page's
// window, each with the route that reached the target or else its longest
// route, and the busiest requesters, targets and relays.

const SEARCH_DELAY_MS = 300;
const TRACEROUTE_SORTS: TracerouteSort[] = ["newest", "route", "distance"];
const DEFAULT_FILTERS: KeptFilters = {
  sort: "newest",
  channel: null,
  minHops: 0,
  successful: false,
};

type NodeMap = ReadonlyMap<number, NodeSummary>;
type ExplorerFilters = Pick<
  TracerouteFilters,
  "sort" | "node" | "nextTo" | "channel" | "minHops" | "successful" | "offset"
>;
type KeptFilters = Pick<
  ExplorerFilters,
  "sort" | "channel" | "minHops" | "successful"
>;

export function TracerouteExplorer({
  since,
  chosenAt,
  pair,
  node,
  windowPicker,
}: {
  since: string | undefined;
  /** When the window was chosen, to keep what is loaded for Back. */
  chosenAt: number;
  pair: [number, number] | null;
  /** The node search, when there is no pair. */
  node: number | null;
  /** The page's window choice, shown among the explorer's filters. */
  windowPicker: ReactNode;
}) {
  // A hop in the URL fills both node searches; a node, the first.
  const linked = pair?.[0] ?? node;
  const [search, setSearch] = useViewState(
    "traceroutes.search",
    linked === null ? "" : formatNodeId(linked),
  );
  const [nextToSearch, setNextToSearch] = useViewState(
    "traceroutes.nextToSearch",
    pair === null ? "" : formatNodeId(pair[1]),
  );
  // The choices besides the searches are kept for the next visit.
  const [kept, keep] = useSetting<KeptFilters>(
    "traceroutes.filters",
    DEFAULT_FILTERS,
    (value) =>
      typeof value === "object" &&
      value !== null &&
      TRACEROUTE_SORTS.includes((value as KeptFilters).sort) &&
      typeof (value as KeptFilters).minHops === "number",
  );
  // A link to a hop or node starts without the kept choices that narrow the
  // runs, so none of its runs is hidden; only the sort is kept.
  const [filters, setFilters] = useViewState<ExplorerFilters>(
    "traceroutes.filters",
    {
      ...(linked === null ? kept : { ...DEFAULT_FILTERS, sort: kept.sort }),
      node: linked === null ? null : formatNodeId(linked),
      nextTo: pair === null ? null : formatNodeId(pair[1]),
      offset: 0,
    },
  );
  const { data, error } = useApi<TracerouteRuns>(
    traceroutesUrl({ ...filters, since }),
    chosenAt,
  );
  const busiest = useApi<BusiestTraceroutes>(
    busiestTraceroutesUrl(since),
    chosenAt,
  );
  const nodes = useMemo(
    () => new Map((data?.nodes ?? []).map((item) => [item.node_id, item])),
    [data],
  );
  const matched = useMemo(() => new Set(data?.matched_node_ids ?? []), [data]);
  const doubtful = useMemo(
    () => new Set(data?.doubtful_node_ids ?? []),
    [data],
  );
  // Any change of filter starts again at the first page. Only a choice made
  // here is kept for the next visit, not one Back brought back.
  const update = (change: Partial<typeof filters>) => {
    const next = { ...filters, offset: 0, ...change };
    setFilters(next);
    const { sort, channel, minHops, successful } = next;
    keep({ sort, channel, minHops, successful });
  };
  const filterNode = (nodeId: number) => {
    setSearch(formatNodeId(nodeId));
    setNextToSearch("");
    update({ node: formatNodeId(nodeId), nextTo: null });
  };
  // A busiest-list node clicked again clears its search.
  const toggleNode = (nodeId: number) => {
    if (filters.node === formatNodeId(nodeId) && filters.nextTo === null) {
      setSearch("");
      update({ node: null });
    } else {
      filterNode(nodeId);
    }
  };

  // Search once typing pauses.
  const searched = search.trim() === "" ? null : search.trim();
  const searchedNextTo =
    nextToSearch.trim() === "" ? null : nextToSearch.trim();
  useEffect(() => {
    if (searched === filters.node && searchedNextTo === filters.nextTo) return;
    const timer = window.setTimeout(
      () =>
        setFilters((current) => ({
          ...current,
          node: searched,
          nextTo: searchedNextTo,
          offset: 0,
        })),
      SEARCH_DELAY_MS,
    );
    return () => window.clearTimeout(timer);
  }, [searched, searchedNextTo, filters.node, filters.nextTo, setFilters]);
  // Each row shows its route through what was searched.
  const searchNote =
    filters.node !== null && filters.nextTo !== null
      ? " with the hop"
      : filters.node !== null || filters.nextTo !== null
        ? " through the searched node"
        : "";

  const channels = busiest.data?.channels ?? [];
  return (
    <>
      <BusiestCards
        busiest={busiest}
        selected={filters.nextTo === null ? filters.node : null}
        onNode={toggleNode}
      />
      <div className="nodes-toolbar">
        <input
          type="search"
          aria-label="Node on the route"
          placeholder="Node on route: name, !hex or number"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <input
          type="search"
          aria-label="Next to node"
          title="Only runs with a hop between the two searched nodes"
          placeholder="Next to node"
          value={nextToSearch}
          onChange={(event) => setNextToSearch(event.target.value)}
        />
        {windowPicker}
        <select
          aria-label="Channel"
          value={filters.channel ?? ""}
          onChange={(event) => update({ channel: event.target.value || null })}
        >
          <option value="">All channels</option>
          {channels.map((item) => (
            <option key={item.channel} value={item.channel}>
              {item.channel} ({item.count.toLocaleString()})
            </option>
          ))}
          {filters.channel !== null &&
          !channels.some((item) => item.channel === filters.channel) ? (
            <option value={filters.channel}>{filters.channel}</option>
          ) : null}
        </select>
        <label>
          Min. hops
          <input
            type="number"
            min={0}
            max={32}
            className="small-number"
            value={filters.minHops}
            onChange={(event) =>
              update({
                minHops: Math.max(
                  0,
                  Math.min(32, Number(event.target.value) || 0),
                ),
              })
            }
          />
        </label>
        {/* Farthest apart always means reached: the backend applies it, so
            unticking it there goes back to latest first. */}
        <label title="Only traceroutes known to have reached their destination">
          <input
            type="checkbox"
            checked={filters.successful || filters.sort === "distance"}
            onChange={(event) =>
              update(
                !event.target.checked && filters.sort === "distance"
                  ? { successful: false, sort: "newest" }
                  : { successful: event.target.checked },
              )
            }
          />
          Reached only
        </label>
      </div>
      {error !== null ? <StatusPanel tone="error">{error}</StatusPanel> : null}
      {data === null && error === null ? (
        <StatusPanel>Reading traceroutes…</StatusPanel>
      ) : null}
      {data !== null ? (
        data.items.length === 0 ? (
          <StatusPanel>No traceroute runs match in this window.</StatusPanel>
        ) : (
          <>
            <RunTable
              runs={data.items}
              searchNote={searchNote}
              matched={matched}
              doubtful={doubtful}
              nodes={nodes}
              onNode={filterNode}
              sort={filters.sort}
              onSort={(sort) => update({ sort })}
            />
            <p className="end-note">
              Runs {(filters.offset + 1).toLocaleString()}–
              {(filters.offset + data.items.length).toLocaleString()}.{" "}
              <button
                type="button"
                className="secondary-button"
                disabled={filters.offset === 0}
                onClick={() =>
                  setFilters({
                    ...filters,
                    offset: Math.max(0, filters.offset - TRACEROUTE_PAGE_SIZE),
                  })
                }
              >
                Previous
              </button>{" "}
              <button
                type="button"
                className="secondary-button"
                disabled={!data.has_more}
                onClick={() =>
                  setFilters({
                    ...filters,
                    offset: filters.offset + TRACEROUTE_PAGE_SIZE,
                  })
                }
              >
                Next
              </button>
            </p>
          </>
        )
      ) : null}
    </>
  );
}

function BusiestCards({
  busiest: { data, error },
  selected,
  onNode,
}: {
  busiest: { data: BusiestTraceroutes | null; error: string | null };
  /** The searched node, marked in the lists when it is one of theirs. */
  selected: string | null;
  onNode: (nodeId: number) => void;
}) {
  const nodes = useMemo(
    () => new Map((data?.nodes ?? []).map((item) => [item.node_id, item])),
    [data],
  );
  // The lists open and close as one, and stay as they were left.
  const [open, setOpen] = useSetting("traceroutes.busiestOpen", true);
  if (error !== null) return <ErrorPanel message={error} />;
  if (data === null) return null;
  const lists: [string, NodeCount[]][] = [
    ["Traceroutes Initiated", data.requesters],
    ["Traceroute Targets", data.targets],
    ["Busy Relays", data.relays],
  ];
  return (
    <details
      className="activity-chart busiest-list"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <h3>Busiest nodes</h3>
      </summary>
      <p className="busiest-hint">Click a node to see every route it is on.</p>
      <div className="links-charts">
        {lists.map(([title, items]) => (
          <section key={title}>
            <h4>{title}</h4>
            {items.length === 0 ? (
              <p className="muted">None in this window.</p>
            ) : (
              <ol className="top-list">
                {items.map((item) => (
                  <li key={item.node_id}>
                    <button
                      type="button"
                      className="top-list-row"
                      aria-pressed={selected === formatNodeId(item.node_id)}
                      title={formatNodeId(item.node_id)}
                      onClick={() => onNode(item.node_id)}
                    >
                      <span className="top-list-name">
                        {nodeName(nodes.get(item.node_id)) ??
                          formatNodeId(item.node_id)}
                      </span>
                      <span className="number">
                        {item.count.toLocaleString()}
                      </span>
                    </button>
                  </li>
                ))}
              </ol>
            )}
          </section>
        ))}
      </div>
    </details>
  );
}

function NodeFilterChip({
  nodeId,
  onNode,
}: {
  nodeId: number;
  onNode: (nodeId: number) => void;
}) {
  return (
    <button
      type="button"
      className="filter-chip"
      title="Show only routes through this node"
      onClick={() => onNode(nodeId)}
    >
      ⌖
    </button>
  );
}

function RunTable({
  runs,
  searchNote,
  matched,
  doubtful,
  nodes,
  onNode,
  sort,
  onSort,
}: {
  runs: TracerouteRun[];
  /** Added to each route's name when it goes through what was searched. */
  searchNote: string;
  /** The searched nodes, marked wherever they are named. */
  matched: ReadonlySet<number>;
  /** Nodes with a doubtful position, whose km get a badge. */
  doubtful: ReadonlySet<number>;
  nodes: NodeMap;
  onNode: (nodeId: number) => void;
  sort: TracerouteSort;
  onSort: (sort: TracerouteSort) => void;
}) {
  const [open, setOpen] = useViewState<number | null>("traceroutes.open", null);
  const [highlightedNode, setHighlightedNode] = useState<number | null>(null);
  const [hoveredHop, setHoveredHop] = useState<DirectedHop | null>(null);
  const [pinned, setPinned] = useState<MinimapFocus>(null);
  const highlight = {
    highlightedNode:
      highlightedNode ??
      (hoveredHop === null && pinned?.kind === "relay" ? pinned.nodeId : null),
    highlightedHop:
      highlightedNode === null
        ? (hoveredHop ?? (pinned?.kind === "hop" ? pinned : null))
        : null,
    onNodeHover: (nodeId: number | null) => {
      setHighlightedNode(nodeId);
      if (nodeId !== null) setHoveredHop(null);
    },
  };
  const names = useMemo(
    () =>
      new Map(
        [...nodes.values()].map((node) => [node.node_id, nodeName(node)]),
      ),
    [nodes],
  );
  // The nodes a route map can place, as routeMinimap does.
  const placed = useMemo(
    () =>
      new Set(
        [...nodes.values()]
          .filter((node) => node.latitude !== null && node.longitude !== null)
          .map((node) => node.node_id),
      ),
    [nodes],
  );
  const endFilter = (nodeId: number) => (
    <NodeFilterChip nodeId={nodeId} onNode={onNode} />
  );
  return (
    <WideTable>
      <table className="packet-table node-table traceroute-table">
        <thead>
          <tr>
            <th scope="col">
              <span className="visually-hidden">Details</span>
            </th>
            <SortHeading
              label="Heard"
              title="Latest first"
              sort="newest"
              active={sort}
              onSort={onSort}
            />
            <th scope="col" className="run-route">
              Route<span className="heading-note">S sender · R target</span>
            </th>
            <SortHeading
              label="Length"
              note="hops · km"
              title="Longest route first"
              sort="route"
              active={sort}
              onSort={onSort}
            />
            <SortHeading
              label="Apart"
              note="km, straight"
              title="Farthest apart first, reached runs only"
              sort="distance"
              active={sort}
              onSort={onSort}
            />
            <th scope="col">Packets</th>
            <th scope="col">Channel</th>
          </tr>
        </thead>
        {/* One tbody per run, so the stripes group a run's rows together. */}
        {runs.map((run) => {
          const key = run.route.packet_row_id;
          // The route's km adds up only its measured hops, so only their
          // nodes can make it doubtful; an unreached target is not one.
          const measured = run.route.path_hops.flatMap((hop) => [
            hop.from_node_id,
            hop.to_node_id,
          ]);
          const toggle = () => {
            setOpen(open === key ? null : key);
            setHighlightedNode(null);
            setHoveredHop(null);
            setPinned(null);
          };
          return (
            <tbody key={key} className="traceroute-run">
              <tr
                className="clickable-row"
                aria-expanded={open === key}
                onClick={(event) => {
                  if ((event.target as Element).closest("a, button")) return;
                  toggle();
                }}
              >
                <td className="run-marker">
                  <button
                    type="button"
                    className="text-button row-details-button"
                    aria-label={
                      open === key
                        ? "Hide this traceroute's details"
                        : "Show this traceroute's details"
                    }
                    aria-expanded={open === key}
                    onClick={toggle}
                  >
                    <Icon name="chevron" />
                    Details
                  </button>
                </td>
                <td>
                  <ShortTime value={run.first_received_at} />
                </td>
                <td className="run-route">
                  <dl>
                    <RouteItem
                      label={`${run.route.is_return ? "Route back" : "Route"}${searchNote}`}
                      path={run.route.path}
                      nodeNames={names}
                      matched={matched}
                      ends={{
                        requester: run.requester_node_id,
                        target: run.target_node_id,
                      }}
                      firstNumber={0}
                      placed={placed}
                      spreadingFactor={run.spreading_factor}
                      hop={directedHop(run.route.hop)}
                      zeroCostHops={zeroCostHops(run.route.hop_costs)}
                      endExtra={endFilter}
                      {...(open === key ? highlight : {})}
                    />
                  </dl>
                </td>
                <td className="number">
                  {run.route.hops} · {formatKm(run.route.km)}
                  <DoubtfulBadge
                    suspects={
                      run.route.km === null
                        ? []
                        : doubtedNames(measured, doubtful, nodes)
                    }
                    unlikely={run.route.unlikely}
                  />
                  <span className="heading-note">
                    {run.route.reached ? "reached" : "not confirmed"}
                  </span>
                </td>
                <td className="number">
                  {formatKm(run.distance_km)}
                  <span className="cell-badges">
                    <DoubtfulBadge
                      suspects={
                        run.doubts.includes("doubtful")
                          ? doubtedNames(
                              [run.requester_node_id, run.target_node_id],
                              doubtful,
                              nodes,
                            )
                          : []
                      }
                      unlikely={run.doubts.includes("terrain")}
                    />
                    {run.doubts.includes("pending") ? <PendingBadge /> : null}
                  </span>
                </td>
                <td>
                  <Packets run={run} />
                </td>
                <td>{run.channel}</td>
              </tr>
              {open === key ? (
                <tr className="links-expanded">
                  <td colSpan={7}>
                    <LegMap
                      run={run}
                      nodes={nodes}
                      names={names}
                      placed={placed}
                      doubted={(nodeIds) =>
                        doubtedNames(nodeIds, doubtful, nodes)
                      }
                      {...highlight}
                      focus={pinned?.kind === "relay" ? pinned : null}
                      onRelayClick={(nodeId) =>
                        setPinned((current) =>
                          current?.kind === "relay" && current.nodeId === nodeId
                            ? null
                            : { kind: "relay", nodeId },
                        )
                      }
                      onHopHover={(hop) => {
                        setHoveredHop(hop);
                        if (hop !== null) setHighlightedNode(null);
                      }}
                      onHopClick={(hop) =>
                        setPinned((current) =>
                          current?.kind === "hop" &&
                          current.fromNodeId === hop.fromNodeId &&
                          current.toNodeId === hop.toNodeId
                            ? null
                            : { kind: "hop", ...hop },
                        )
                      }
                    />
                  </td>
                </tr>
              ) : null}
            </tbody>
          );
        })}
      </table>
    </WideTable>
  );
}

// The searched hop, as the run's route crossed it.
function directedHop(hop: TracerouteRun["route"]["hop"]): DirectedHop | null {
  return hop == null
    ? null
    : { fromNodeId: hop.from_node_id, toNodeId: hop.to_node_id };
}

// A run's packet, opened on the searched hop when there is one. A packet
// without it shows all its reports.
function runPacketHref(run: TracerouteRun, packetRowId: number): string {
  const hop = run.route.hop;
  return hop == null
    ? `#packets/${packetRowId}`
    : packetHopHref(packetRowId, hop.from_node_id, hop.to_node_id);
}

function Packets({ run }: { run: TracerouteRun }) {
  const request =
    run.request_packet_row_id !== null ? (
      <a
        className="text-button"
        href={runPacketHref(run, run.request_packet_row_id)}
      >
        request
      </a>
    ) : (
      <span className="muted">request not heard</span>
    );
  const reply =
    run.reply_packet_row_id !== null ? (
      <a
        className="text-button"
        href={runPacketHref(run, run.reply_packet_row_id)}
      >
        reply
      </a>
    ) : (
      <span className="muted">no reply heard</span>
    );
  return (
    <>
      {request} · {reply}
    </>
  );
}

/** The run's route on its own, from the row's own data. */
function LegMap({
  run,
  nodes,
  names,
  placed,
  doubted,
  highlightedNode,
  highlightedHop,
  onNodeHover,
  focus,
  onRelayClick,
  onHopHover,
  onHopClick,
}: {
  run: TracerouteRun;
  nodes: NodeMap;
  names: ReadonlyMap<number, string | null>;
  placed: ReadonlySet<number>;
  doubted: (nodeIds: number[]) => string[];
  focus: MinimapFocus;
  onRelayClick: (nodeId: number) => void;
  onHopHover: (hop: DirectedHop | null) => void;
  onHopClick: (hop: DirectedHop) => void;
} & NodeHighlight) {
  const contents = useMemo(() => routeMinimap(run, nodes), [run, nodes]);
  const at = useMemo(
    () => new Map(run.positions.map((heard) => [heard.node_id, heard])),
    [run],
  );
  // With a hop searched, the packet opens on the reports with the hop,
  // in the way this route crossed it.
  const hop = run.route.hop;
  return (
    <div className="traceroute-map">
      {contents.markers.length === 0 ? (
        <p className="muted">No node on this route has a known position.</p>
      ) : (
        <Suspense fallback={<p className="muted">Loading map…</p>}>
          <PacketMinimap
            contents={contents}
            focus={focus}
            highlightedNode={highlightedNode}
            highlightedHop={highlightedHop}
            onNodeHover={onNodeHover}
            onRelayClick={onRelayClick}
            onHopHover={onHopHover}
            onHopClick={onHopClick}
            showLegend={false}
          />
        </Suspense>
      )}
      <div className="traceroute-hops">
        <RouteHopList
          route={run.route}
          nodeNames={names}
          ends={{
            requester: run.requester_node_id,
            target: run.target_node_id,
          }}
          placed={placed}
          spreadingFactor={run.spreading_factor}
          at={at}
          doubted={doubted}
          highlightedNode={highlightedNode}
          highlightedHop={highlightedHop}
          onNodeHover={onNodeHover}
        />
        <a
          className="text-button"
          href={runPacketHref(run, run.route.packet_row_id)}
        >
          {hop === null ? "View all routes" : "View all routes with this hop"}
        </a>
      </div>
    </div>
  );
}
