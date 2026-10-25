import type { CoverageNodeList, CoverageViewNode, MapNode } from "./api";
import { marginColor } from "./coverageLayer";
import { formatNodeId } from "./nodeDisplay";

/**
 * The nodes surveys heard in the coverage cells in view, in most cells
 * first, then the unknown relays.
 */
export function HeardInView({
  id,
  page,
  error,
  selectedNodeId,
  drawn,
  loadedIds,
  onChoose,
}: {
  id?: string;
  page: CoverageNodeList | null;
  error: string | null;
  selectedNodeId: number | null;
  drawn: ReadonlyMap<number, MapNode>;
  /** Nodes with a recent position, drawn or hidden by the filters. */
  loadedIds: ReadonlySet<number>;
  onChoose: (node: CoverageViewNode) => void;
}) {
  return (
    <section
      id={id}
      className="sidebar-list heard-in-view"
      aria-label="Coverage"
    >
      {error !== null ? (
        <p className="sidebar-note">Coverage nodes could not load. {error}</p>
      ) : page === null ? (
        <p className="sidebar-note">Loading…</p>
      ) : page.nodes.length === 0 && page.unknown === 0 ? (
        <p className="sidebar-note">No measurements in view.</p>
      ) : (
        <ul>
          {page.nodes.map((heard) => {
            const node = drawn.get(heard.node_id);
            const hidden = node === undefined && loadedIds.has(heard.node_id);
            return (
              <li key={heard.node_id}>
                <button
                  type="button"
                  data-node-id={heard.node_id}
                  className={node === undefined ? "unplaced" : undefined}
                  aria-pressed={heard.node_id === selectedNodeId}
                  aria-disabled={hidden}
                  title={
                    node !== undefined
                      ? undefined
                      : loadedIds.has(heard.node_id)
                        ? "Hidden by the map's filters"
                        : "No recent position, not on the map"
                  }
                  onClick={() => !hidden && onChoose(heard)}
                >
                  <span
                    className="coverage-margin-dot"
                    style={{ background: marginColor(heard.average_margin) }}
                  />
                  <span className="chatty-name">
                    <span>
                      {heard.long_name ?? formatNodeId(heard.node_id)}
                    </span>
                  </span>
                  <span className="chatty-count">
                    {heard.cells.toLocaleString()}
                  </span>
                </button>
              </li>
            );
          })}
          {page.unknown === 0 ? null : (
            <li className="heard-unknown">
              <span />
              <span className="muted">Unknown relays</span>
              <span className="chatty-count muted">
                {page.unknown.toLocaleString()} measurements
              </span>
            </li>
          )}
          {page.truncated ? (
            <li className="heard-unknown">
              <span />
              <span className="muted">Only the 200 nodes in most cells</span>
            </li>
          ) : null}
        </ul>
      )}
    </section>
  );
}
