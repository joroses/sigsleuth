import { useEffect, useState } from "react";

import { NodeListItem, NodeListPage, nodeSearchUrl } from "./api";
import { nodeLabel } from "./nodeDisplay";
import { NodeSearch } from "./NodeSearch";
import { IconButton, NodeLink } from "./ui";
import { useApi } from "./useApi";
import { useNodeName } from "./useNodeName";

// A search is sent once typing pauses this long, not one per letter.
const SEARCH_DELAY_MS = 300;
const NO_NODES: NodeListItem[] = [];

/**
 * A node chosen to filter by, with a button to clear it, or a search of
 * every known node to choose one.
 */
export function NodeFilter({
  label,
  nodeId,
  onPick,
}: {
  label: string;
  nodeId: number | null;
  onPick: (nodeId: number | null) => void;
}) {
  // A node picked from the search brings its name; one kept from before, or
  // typed as an ID, is looked up.
  const [picked, setPicked] = useState<NodeListItem | null>(null);
  const pickedName = picked?.node_id === nodeId ? nodeLabel(picked) : null;
  const foundName = useNodeName(pickedName === null ? nodeId : null);
  return (
    <div className="node-filter">
      <span className="node-filter-label">{label}</span>
      {nodeId === null ? (
        <FilterSearch
          label={label}
          onSelect={(node) => {
            setPicked(node);
            onPick(node.node_id);
          }}
          onSelectId={onPick}
        />
      ) : (
        <span className="los-chosen">
          <NodeLink nodeId={nodeId} name={pickedName ?? foundName} />
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

function FilterSearch({
  label,
  onSelect,
  onSelectId,
}: {
  label: string;
  onSelect: (node: NodeListItem) => void;
  onSelectId: (nodeId: number) => void;
}) {
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    if (typed === query) return;
    const timer = window.setTimeout(() => setQuery(typed), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [typed, query]);
  const { data, error, loading } = useApi<NodeListPage>(
    query === "" ? null : nodeSearchUrl(query),
  );
  // The last matches stay, narrowed by what is typed, while the next load.
  const [last, setLast] = useState<NodeListPage | null>(null);
  if (data !== null && data !== last) setLast(data);
  return (
    <NodeSearch
      nodes={(data ?? last)?.items ?? NO_NODES}
      label={label}
      onSelect={onSelect}
      onSelectId={onSelectId}
      onQuery={(text) => setTyped(text.trim())}
      emptyText={
        typed !== query || loading ? null : (error ?? "No matching nodes.")
      }
    />
  );
}
