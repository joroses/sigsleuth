import { Fragment, Suspense, lazy, useMemo } from "react";

import { LinkKind, NodeSummary, LinkRow, LinkTable, linkTableUrl } from "./api";
import { ExplorerCell, LinkCells, LinkHeadings, MoreRows } from "./LinkColumns";
import {
  LINK_KIND_LABELS,
  type LinkSort,
  isLinkSort,
  sortLinks,
  weakerMargin,
} from "./linkRows";
import { LinkDetails } from "./LinkDetails";
import { SEEN_WINDOWS, type SeenWindow } from "./mapTimeFilter";
import { formatNodeId } from "./nodeDisplay";
import { nodeSearchIndex, searchNodes } from "./nodeSearch";
import { TracerouteExplorer } from "./TracerouteExplorer";
import { useSetting, useViewState } from "./remember";
import { openedAt, useApi } from "./useApi";
import {
  ErrorPanel,
  Icon,
  IconButton,
  NodeName,
  OpenedAt,
  StatusPanel,
  WideTable,
} from "./ui";

// Two tabs over one window: the links heard between nodes, and the traceroute
// runs with the routes that reached the target, or their longest routes
// (TracerouteExplorer.tsx).
// A third, line of sight (LineOfSightView.tsx), needs no window.

export type LinksTab = "links" | "traceroutes" | "line-of-sight";

const TABS: {
  tab: LinksTab;
  icon: "links" | "route" | "profile";
  label: string;
  href: string;
}[] = [
  { tab: "links", icon: "links", label: "Links", href: "#links" },
  {
    tab: "traceroutes",
    icon: "route",
    label: "Traceroute explorer",
    href: "#links/traceroutes",
  },
  {
    tab: "line-of-sight",
    icon: "profile",
    label: "Line of sight",
    href: "#links/line-of-sight",
  },
];

// Line of sight brings in MapLibre, so it loads only on its own tab.
const LineOfSightView = lazy(() =>
  import("./LineOfSightView").then((module) => ({
    default: module.LineOfSightView,
  })),
);

const HOUR_MS = 60 * 60 * 1000;
const ROWS_STEP = 100;

type NodeMap = ReadonlyMap<number, NodeSummary>;

export function LinksView({
  tab,
  pair = null,
  node = null,
  at = null,
}: {
  tab: LinksTab;
  /** On the explorer, the two node searches, for runs with a hop between them; on line of sight, the two ends. */
  pair?: [number, number] | null;
  /** On the links and the explorer, the node search. */
  node?: number | null;
  /** On line of sight, the time in ms to place the two ends at. */
  at?: number | null;
}) {
  return (
    <section className="links-page" aria-label="Links">
      <nav className="links-tabs" aria-label="Links views">
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
      {tab === "line-of-sight" ? (
        <Suspense fallback={<StatusPanel>Loading…</StatusPanel>}>
          <LineOfSightView pair={pair} at={at} />
        </Suspense>
      ) : (
        <WindowTabs
          key={pair?.join("-") ?? node ?? ""}
          tab={tab}
          pair={pair}
          node={node}
        />
      )}
    </section>
  );
}

function WindowTabs({
  tab,
  pair,
  node,
}: {
  tab: "links" | "traceroutes";
  pair: [number, number] | null;
  node: number | null;
}) {
  // The window starts when it is chosen, so its URL stays the same while
  // shown, or when it was last chosen if that was minutes ago (openedAt).
  const [savedWindow, saveWindow] = useSetting<SeenWindow>(
    "links.window",
    "24h",
    (value) => SEEN_WINDOWS.some((item) => item.id === value),
  );
  // A link to a hop's or node's runs opens on all history, which their
  // searches keep quick, so a shorter kept window hides none of them; a link
  // to a node's links opens on 7 days, as all of them is not timed. Neither
  // window is kept. The window stays in this history entry, so Back shows
  // what was loaded for it.
  const [{ windowId, since, chosenAt }, setWindow] = useViewState(
    "links.window",
    () =>
      windowStart(
        tab === "traceroutes" && (pair !== null || node !== null)
          ? "all"
          : node !== null
            ? "7d"
            : savedWindow,
      ),
  );
  const [linkKind, setLinkKind] = useSetting<LinkKind>(
    "links.kind",
    "traceroute",
    (value) => ["traceroute", "direct", "neighbor"].includes(value as string),
  );

  const windowPicker = (
    <>
      <label>
        Window
        <select
          value={windowId}
          onChange={(event) => {
            const next = event.target.value as SeenWindow;
            saveWindow(next);
            setWindow(windowStart(next));
          }}
        >
          {SEEN_WINDOWS.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <OpenedAt
        at={chosenAt}
        onRefresh={() => setWindow(windowStart(windowId, true))}
      />
    </>
  );

  // The explorer places the window among its own filters.
  if (tab === "traceroutes") {
    return (
      <TracerouteExplorer
        key={`${since}-${pair}`}
        since={since}
        chosenAt={chosenAt}
        pair={pair}
        node={node}
        windowPicker={windowPicker}
      />
    );
  }
  return (
    <>
      <div className="nodes-toolbar">
        {windowPicker}
        <label>
          Link kind
          <select
            value={linkKind}
            onChange={(event) => setLinkKind(event.target.value as LinkKind)}
          >
            {Object.entries(LINK_KIND_LABELS).map(([kind, label]) => (
              <option key={kind} value={kind}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <LinkTabs
        linkKind={linkKind}
        since={since}
        chosenAt={chosenAt}
        node={node}
      />
    </>
  );
}

function windowStart(
  windowId: SeenWindow,
  fresh = false,
): {
  windowId: SeenWindow;
  since: string | undefined;
  /** When the window was chosen; what is loaded for it is kept under it. */
  chosenAt: number;
} {
  const hours = SEEN_WINDOWS.find((item) => item.id === windowId)?.hours;
  const chosenAt = openedAt(`links.${windowId}`, fresh);
  return {
    windowId,
    since:
      hours == null
        ? undefined
        : new Date(chosenAt - hours * HOUR_MS).toISOString(),
    chosenAt,
  };
}

function LinkTabs({
  linkKind,
  since,
  chosenAt,
  node,
}: {
  linkKind: LinkKind;
  since: string | undefined;
  chosenAt: number;
  node: number | null;
}) {
  const { data, error } = useApi<LinkTable>(
    linkTableUrl(linkKind, since),
    chosenAt,
  );
  const nodes = useMemo(
    () => new Map((data?.nodes ?? []).map((node) => [node.node_id, node])),
    [data],
  );
  const doubtful = useMemo(
    () => new Set(data?.doubtful_node_ids ?? []),
    [data],
  );
  if (error !== null) return <StatusPanel tone="error">{error}</StatusPanel>;
  if (data === null) return <StatusPanel>Loading links…</StatusPanel>;
  if (data.items.length === 0) {
    return <StatusPanel>No links in this window.</StatusPanel>;
  }
  return (
    <>
      {data.truncated ? (
        <ErrorPanel message="Only the 20,000 most recently seen links are shown." />
      ) : null}
      <LinkRanking
        links={data.items}
        nodes={nodes}
        doubtful={doubtful}
        linkKind={linkKind}
        since={since}
        node={node}
      />
    </>
  );
}

/**
 * A filter kept for the next visit, which a link to a node starts at `all`
 * instead, so none of the node's links is hidden. A choice made here is kept.
 */
function useLinkFilter<T>(
  key: string,
  initial: T,
  all: T,
  linked: boolean,
  valid?: (value: unknown) => boolean,
) {
  const [kept, keep] = useSetting(key, initial, valid);
  const [value, setValue] = useViewState(key, linked ? all : kept);
  const choose = (next: T) => {
    setValue(next);
    keep(next);
  };
  return [value, choose] as const;
}

// "Weaker side at least" choices in dB of fade margin; null keeps every link,
// including those without an SNR.
const MARGIN_FLOORS = [null, 0, 4, 10, 15, 20, 30];

function LinkRanking({
  links,
  nodes,
  doubtful,
  linkKind,
  since,
  node,
}: {
  links: LinkRow[];
  nodes: NodeMap;
  /** Nodes with a doubtful position; by distance their links rank after the
   * measured ones, or first (still longest first) when shortest first. */
  doubtful: ReadonlySet<number>;
  linkKind: LinkKind;
  since: string | undefined;
  /** A node in the URL fills the search. */
  node: number | null;
}) {
  const [search, setSearch] = useViewState(
    "links.search",
    node === null ? "" : formatNodeId(node),
  );
  const linked = node !== null;
  const [minObservations, setMinObservations] = useLinkFilter(
    "links.minObservations",
    5,
    1,
    linked,
  );
  const [marginFloor, setMarginFloor] = useLinkFilter<number | null>(
    "links.marginFloor",
    null,
    null,
    linked,
    (value) => MARGIN_FLOORS.includes(value as number | null),
  );
  const [savedChannel, setChannel] = useLinkFilter(
    "links.channel",
    "",
    "",
    linked,
  );
  const [sort, setSort] = useSetting<LinkSort>(
    "links.sort",
    "distance",
    isLinkSort,
  );
  const [ascending, setAscending] = useSetting("links.ascending", false);
  // Clicking the active column again turns its order round.
  const chooseSort = (next: LinkSort) => {
    setAscending(next === sort && !ascending);
    setSort(next);
  };
  const [shown, setShown] = useViewState("links.shown", ROWS_STEP);
  // The traceroute link whose packets are listed under its row.
  const [open, setOpen] = useViewState<string | null>("links.open", null);

  const channels = useMemo(
    () => countBy(links, (link) => link.channel),
    [links],
  );
  // A kept channel these links do not have shows them all.
  const channel = channels.some(([name]) => name === savedChannel)
    ? savedChannel
    : "";
  // Every end of a link, named or not, so an unnamed node is found by its ID.
  const searchIndex = useMemo(() => {
    const ids = new Set(links.flatMap((link) => [link.node_a, link.node_b]));
    return nodeSearchIndex(
      [...ids].map((nodeId) => ({
        node_id: nodeId,
        long_name: nodes.get(nodeId)?.long_name ?? null,
        short_name: nodes.get(nodeId)?.short_name ?? null,
      })),
    );
  }, [links, nodes]);
  const sorted = useMemo(() => {
    const found =
      search.trim() === ""
        ? null
        : new Set(
            searchNodes(searchIndex, search, Infinity).map(
              (node) => node.node_id,
            ),
          );
    const filtered = links.filter((link) => {
      const margin = weakerMargin(link);
      return (
        totalObservations(link) >= minObservations &&
        (marginFloor === null || (margin !== null && margin >= marginFloor)) &&
        (channel === "" || link.channel === channel) &&
        (found === null || found.has(link.node_a) || found.has(link.node_b))
      );
    });
    return sortLinks(filtered, sort, ascending);
  }, [
    ascending,
    links,
    searchIndex,
    search,
    minObservations,
    marginFloor,
    channel,
    sort,
  ]);
  const traceroutes = linkKind === "traceroute";

  return (
    <>
      <div className="nodes-toolbar">
        <input
          type="search"
          aria-label="Search links by node"
          placeholder="Node name, !hex or number"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          aria-label="Channel"
          value={channel}
          onChange={(event) => setChannel(event.target.value)}
        >
          <option value="">All channels</option>
          {channels.map(([name, count]) => (
            <option key={name} value={name}>
              {name} ({count})
            </option>
          ))}
        </select>
        <label>
          Min. observations
          <input
            type="number"
            min={1}
            value={minObservations}
            className="small-number"
            onChange={(event) =>
              setMinObservations(Math.max(1, Number(event.target.value) || 1))
            }
          />
        </label>
        <label>
          Weaker side at least
          <select
            value={marginFloor ?? ""}
            onChange={(event) =>
              setMarginFloor(
                event.target.value === "" ? null : Number(event.target.value),
              )
            }
          >
            {MARGIN_FLOORS.map((floor) => (
              <option key={floor ?? "any"} value={floor ?? ""}>
                {floor === null ? "any" : `${floor} dB margin`}
              </option>
            ))}
          </select>
        </label>
      </div>
      {sorted.length === 0 ? (
        <StatusPanel>No links match these filters in this window.</StatusPanel>
      ) : (
        <>
          <WideTable>
            <table className="packet-table node-table links-table">
              <thead>
                <tr>
                  <th scope="col">Node A</th>
                  <th scope="col">Node B</th>
                  {traceroutes ? <th scope="col">Explorer</th> : null}
                  <LinkHeadings
                    sort={sort}
                    ascending={ascending}
                    onSort={chooseSort}
                    ways="A ⇄ B"
                    waysTitle="A heard by B ⇄ B heard by A"
                  />
                  <th scope="col">Details</th>
                </tr>
              </thead>
              <tbody>
                {sorted.slice(0, shown).map((link) => {
                  const key = `${link.node_a}-${link.node_b}:${link.channel}`;
                  // The kind too, so another kind's row of the pair stays shut.
                  const openKey = `${linkKind}:${key}`;
                  const expanded = open === openKey;
                  const toggle = () => setOpen(expanded ? null : openKey);
                  return (
                    <Fragment key={key}>
                      <tr
                        className="clickable-row"
                        onClick={(event) => {
                          if ((event.target as Element).closest("a, button"))
                            return;
                          toggle();
                        }}
                      >
                        <td className="link-node">
                          <NodeName nodeId={link.node_a} nodes={nodes} />
                        </td>
                        <td className="link-node">
                          <NodeName nodeId={link.node_b} nodes={nodes} />
                        </td>
                        {traceroutes ? <ExplorerCell link={link} /> : null}
                        <LinkCells
                          link={link}
                          nodes={nodes}
                          doubtful={doubtful}
                        />
                        <td className="expand-cell">
                          <IconButton
                            icon="chevron"
                            label={
                              expanded
                                ? "Hide this link's details"
                                : "Show this link's details"
                            }
                            aria-expanded={expanded}
                            onClick={toggle}
                          />
                        </td>
                      </tr>
                      {expanded ? (
                        <tr className="links-expanded">
                          <td colSpan={11}>
                            <LinkDetails
                              link={link}
                              linkKind={linkKind}
                              nodes={nodes}
                              since={since}
                            />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </WideTable>
          <MoreRows
            shown={shown}
            total={sorted.length}
            onMore={() => setShown(shown + ROWS_STEP)}
          />
        </>
      )}
    </>
  );
}

function totalObservations(link: LinkRow): number {
  return link.a_to_b.observations + link.b_to_a.observations;
}

function countBy<T>(items: T[], key: (item: T) => string): [string, number][] {
  const counts = new Map<string, number>();
  for (const item of items)
    counts.set(key(item), (counts.get(key(item)) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1]);
}
