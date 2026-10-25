import type { TextSender, TextSenderList } from "./api";
import { formatNodeId } from "./nodeDisplay";

/**
 * Nodes whose broadcast text messages several gateways reported in the
 * window, most first, each with its region, such as Śląskie. Those not in
 * placedIds have no recent position.
 */
export function ChattyNodes({
  id,
  page,
  error,
  selectedNodeId,
  placedIds,
  onChoose,
}: {
  id?: string;
  page: TextSenderList | null;
  error: string | null;
  selectedNodeId: number | null;
  placedIds: ReadonlySet<number>;
  onChoose: (sender: TextSender) => void;
}) {
  return (
    <section id={id} className="sidebar-list" aria-label="Chatty nodes">
      {error !== null ? (
        <p className="sidebar-note">Chatty nodes could not load. {error}</p>
      ) : page === null ? (
        <p className="sidebar-note">Loading…</p>
      ) : page.items.length === 0 ? (
        <p className="sidebar-note">
          No node's texts reached several gateways in the window.
        </p>
      ) : (
        <ul>
          {page.items.map((sender) => {
            const placed = placedIds.has(sender.node_id);
            return (
              <li key={sender.node_id}>
                <button
                  type="button"
                  data-node-id={sender.node_id}
                  className={placed ? undefined : "unplaced"}
                  aria-pressed={sender.node_id === selectedNodeId}
                  title={
                    placed ? undefined : "No recent position, not on the map"
                  }
                  onClick={() => onChoose(sender)}
                >
                  <span className="chatty-name">
                    <span>
                      {sender.long_name ?? formatNodeId(sender.node_id)}
                    </span>
                  </span>
                  <span className="chatty-region">{sender.region ?? "—"}</span>
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
