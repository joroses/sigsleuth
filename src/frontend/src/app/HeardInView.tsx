import { useEffect, useRef, useState } from "react";

import type { CoverageNodeList, MapNode } from "./api";
import { marginColor } from "./coverageLayer";
import { formatNodeId } from "./nodeDisplay";

/**
 * The nodes surveys heard in the coverage cells in view, in most cells
 * first, then the unknown relays. Nodes the map draws can be chosen. The
 * list folds away under its heading.
 */
export function HeardInView({
  page,
  error,
  selectedNodeId,
  drawn,
  loadedIds,
  onChoose,
}: {
  page: CoverageNodeList | null;
  error: string | null;
  selectedNodeId: number | null;
  drawn: ReadonlyMap<number, MapNode>;
  /** Nodes with a recent position, drawn or hidden by the filters. */
  loadedIds: ReadonlySet<number>;
  onChoose: (node: MapNode) => void;
}) {
  // A node chosen elsewhere, such as on the map, is scrolled into view.
  const list = useRef<HTMLUListElement | null>(null);
  const [open, setOpen] = useState(true);
  useEffect(() => {
    list.current
      ?.querySelector('[aria-pressed="true"]')
      ?.scrollIntoView?.({ block: "nearest" });
  }, [selectedNodeId, page, open]);
  // With no measurements in view the section goes; the coverage panel says so.
  if (error === null && page !== null && isEmpty(page)) return null;
  // Only the list needs the minimum height; a note or a folded list does not.
  const listShown = open && error === null && page !== null;
  return (
    <section
      className={`chatty-nodes heard-in-view ${listShown ? "" : "chatty-nodes-closed"}`}
      aria-label="Heard in view"
    >
      <div className="chatty-heading">
        <button
          type="button"
          className="section-label chatty-toggle"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          Heard in view
        </button>
      </div>
      {!open ? null : error !== null ? (
        <p className="sidebar-note">Heard nodes could not load. {error}</p>
      ) : page === null ? (
        <p className="sidebar-note">Loading…</p>
      ) : (
        <ul ref={list}>
          {page.nodes.map((heard) => {
            const node = drawn.get(heard.node_id);
            return (
              <li key={heard.node_id}>
                <button
                  type="button"
                  aria-pressed={heard.node_id === selectedNodeId}
                  aria-disabled={node === undefined}
                  title={
                    node !== undefined
                      ? undefined
                      : loadedIds.has(heard.node_id)
                        ? "Hidden by the map's filters"
                        : "No recent position; not on the map"
                  }
                  onClick={() => node !== undefined && onChoose(node)}
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
        </ul>
      )}
      {open && page?.truncated ? (
        <p className="sidebar-note">Only the 200 nodes in most cells.</p>
      ) : null}
    </section>
  );
}

function isEmpty(page: CoverageNodeList): boolean {
  return page.nodes.length === 0 && page.unknown === 0;
}
