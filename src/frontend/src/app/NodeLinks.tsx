import { Fragment, useMemo } from "react";

import type { LinkKind, LinkTable } from "./api";
import { ExplorerCell, LinkCells, LinkHeadings, MoreRows } from "./LinkColumns";
import {
  LINK_KIND_LABELS,
  type LinkSort,
  fromNode,
  isLinkSort,
  sortLinks,
} from "./linkRows";
import { LinkDetails } from "./LinkDetails";
import {
  CONTACT_DAYS,
  zeroCostKey,
  zeroCostLabel,
  zeroCostTitle,
  zeroCostWays,
} from "./nodeContacts";
import { formatNodeId } from "./nodeDisplay";
import { nodeSearchIndex, searchNodes } from "./nodeSearch";
import { useSetting, useViewState } from "./remember";
import {
  Badge,
  ErrorPanel,
  IconButton,
  NodeName,
  StatusPanel,
  WideTable,
} from "./ui";
import type { ApiState } from "./useApi";

const ROWS_STEP = 10;

/**
 * A node page's links of every kind over the last CONTACT_DAYS, with the
 * Links page's columns, including those to nodes with no position. The page
 * loads them (nodeLinksUrl, from `since`), as its minimap draws them too.
 */
export function NodeLinks({
  nodeId,
  links: { data, error },
  since,
}: {
  nodeId: number;
  links: ApiState<LinkTable>;
  since: Date;
}) {
  const [search, setSearch] = useViewState("node.links.search", "");
  const [kind, setKind] = useViewState<LinkKind | "">("node.links.kind", "");
  // Most heard first: by distance, links with no position would come last.
  const [sort, setSort] = useSetting<LinkSort>(
    "node.links.sort",
    "observations",
    isLinkSort,
  );
  const [ascending, setAscending] = useSetting("node.links.ascending", false);
  const chooseSort = (next: LinkSort) => {
    setAscending(next === sort && !ascending);
    setSort(next);
  };
  const [shown, setShown] = useViewState("node.links.shown", ROWS_STEP);
  const [open, setOpen] = useViewState<string | null>("node.links.open", null);

  const nodes = useMemo(
    () => new Map((data?.nodes ?? []).map((node) => [node.node_id, node])),
    [data],
  );
  const doubtful = useMemo(
    () => new Set(data?.doubtful_node_ids ?? []),
    [data],
  );
  const links = useMemo(
    () => (data?.items ?? []).map((link) => fromNode(link, nodeId)),
    [data, nodeId],
  );
  const zeroCost = useMemo(
    () => zeroCostWays(nodeId, data?.zero_cost_hops ?? []),
    [data, nodeId],
  );
  // The other ends, named or not, so an unnamed node is found by its ID.
  const searchIndex = useMemo(
    () =>
      nodeSearchIndex(
        [...new Set(links.map((link) => link.node_b))].map((other) => ({
          node_id: other,
          long_name: nodes.get(other)?.long_name ?? null,
          short_name: nodes.get(other)?.short_name ?? null,
        })),
      ),
    [links, nodes],
  );
  const sorted = useMemo(() => {
    const found =
      search.trim() === ""
        ? null
        : new Set(
            searchNodes(searchIndex, search, Infinity).map(
              (node) => node.node_id,
            ),
          );
    return sortLinks(
      links.filter(
        (link) =>
          (kind === "" || link.link_kind === kind) &&
          (found === null || found.has(link.node_b)),
      ),
      sort,
      ascending,
    );
  }, [links, searchIndex, search, kind, sort, ascending]);

  return (
    <>
      <div className="section-intro compact heading-with-filter">
        <h3>Links</h3>
        {links.length > 0 ? (
          <div className="nodes-toolbar">
            <input
              type="search"
              aria-label="Search links by node"
              placeholder="Node name, !hex or number"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <select
              aria-label="Link kind"
              value={kind}
              onChange={(event) => setKind(event.target.value as LinkKind | "")}
            >
              <option value="">All link kinds ({links.length})</option>
              {Object.entries(LINK_KIND_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label} (
                  {links.filter((link) => link.link_kind === value).length})
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>
      {error !== null ? <StatusPanel tone="error">{error}</StatusPanel> : null}
      {data === null && error === null ? (
        <StatusPanel>Loading links…</StatusPanel>
      ) : null}
      {data !== null && data.truncated ? (
        <ErrorPanel message="Only the 20,000 most recently seen links of a kind are shown." />
      ) : null}
      {data !== null && links.length === 0 ? (
        <StatusPanel>No links in the last {CONTACT_DAYS} days.</StatusPanel>
      ) : null}
      {links.length > 0 && sorted.length === 0 ? (
        <StatusPanel>No links match these filters.</StatusPanel>
      ) : null}
      {sorted.length > 0 ? (
        <>
          <WideTable>
            <table className="packet-table node-table links-table">
              <thead>
                <tr>
                  <th scope="col" className="link-node">
                    Node
                  </th>
                  <th scope="col" className="link-node">
                    ID
                  </th>
                  <th scope="col">Link kind</th>
                  <th scope="col">Explorer</th>
                  <LinkHeadings
                    sort={sort}
                    ascending={ascending}
                    onSort={chooseSort}
                    ways="out ⇄ in"
                    waysTitle="this node heard by the other ⇄ the other heard by this node"
                  />
                  <th scope="col">Details</th>
                </tr>
              </thead>
              <tbody>
                {sorted.slice(0, shown).map((link) => {
                  const key = `${link.link_kind}:${link.node_b}:${link.channel}`;
                  const expanded = open === key;
                  const free =
                    link.link_kind === "traceroute"
                      ? zeroCost.get(zeroCostKey(link.node_b, link.channel))
                      : undefined;
                  const toggle = () => setOpen(expanded ? null : key);
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
                          <NodeName nodeId={link.node_b} nodes={nodes} />
                        </td>
                        <td className="link-node mono muted">
                          {formatNodeId(link.node_b)}
                        </td>
                        <td>
                          {LINK_KIND_LABELS[link.link_kind]}
                          {free === undefined ? null : (
                            <>
                              {" "}
                              <Badge tone="blue" title={zeroCostTitle(free)}>
                                {zeroCostLabel(free)}
                              </Badge>
                            </>
                          )}
                        </td>
                        <ExplorerCell link={link} />
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
                          <td colSpan={12}>
                            <LinkDetails
                              link={link}
                              linkKind={link.link_kind}
                              nodes={nodes}
                              since={since.toISOString()}
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
      ) : null}
    </>
  );
}
