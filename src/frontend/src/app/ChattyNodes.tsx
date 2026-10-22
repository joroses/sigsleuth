import { useEffect, useRef, useState } from "react";

import type { TextSender, TextSenderList } from "./api";
import { formatNodeId } from "./nodeDisplay";

/**
 * Nodes whose broadcast text messages several gateways reported in the
 * window, most first, each with its region, such as Śląskie. Those not in placedIds have no recent
 * position and are marked. The list folds away under its heading.
 */
export function ChattyNodes({
  page,
  error,
  selectedNodeId,
  placedIds,
  onChoose,
}: {
  page: TextSenderList | null;
  error: string | null;
  selectedNodeId: number | null;
  placedIds: ReadonlySet<number>;
  onChoose: (sender: TextSender) => void;
}) {
  // A node chosen elsewhere, such as on the map, is scrolled into view.
  const list = useRef<HTMLUListElement | null>(null);
  const [open, setOpen] = useState(true);
  useEffect(() => {
    list.current
      ?.querySelector('[aria-pressed="true"]')
      ?.scrollIntoView?.({ block: "nearest" });
  }, [selectedNodeId, page, open]);
  return (
    <section
      className={`chatty-nodes ${open ? "" : "chatty-nodes-closed"}`}
      aria-label="Chatty nodes"
    >
      <div className="chatty-heading">
        <button
          type="button"
          className="section-label chatty-toggle"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          Chatty nodes
        </button>
        {open ? (
          <span className="chatty-hint">Select to see text reach</span>
        ) : null}
      </div>
      {!open ? null : error !== null ? (
        <p className="sidebar-note">Chatty nodes could not load. {error}</p>
      ) : page === null ? (
        <p className="sidebar-note">Loading…</p>
      ) : page.items.length === 0 ? (
        <p className="sidebar-note">
          No node's texts reached several gateways in the window.
        </p>
      ) : (
        <ul ref={list}>
          {page.items.map((sender) => {
            const placed = placedIds.has(sender.node_id);
            return (
              <li key={sender.node_id}>
                <button
                  type="button"
                  aria-pressed={sender.node_id === selectedNodeId}
                  title={
                    placed ? undefined : "No recent position; not on the map"
                  }
                  onClick={() => onChoose(sender)}
                >
                  <strong className="chatty-region">
                    {sender.region ?? "—"}
                  </strong>
                  <span className="chatty-name">
                    <span>
                      {sender.long_name ?? formatNodeId(sender.node_id)}
                    </span>
                    {placed ? null : (
                      <span
                        className="chatty-unplaced"
                        aria-label="no position"
                      >
                        ○
                      </span>
                    )}
                  </span>
                  <span className="chatty-count">
                    {sender.messages.toLocaleString()}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
