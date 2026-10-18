import { KeyboardEvent, useId, useMemo, useState } from "react";

import type { MapNode } from "./api";
import { formatNodeId, nodeLabel, nodeMarkerLabel } from "./nodeDisplay";
import { nodeSearchIndex, searchNodes } from "./nodeSearch";

/** A search box over the nodes on the map; ↑/↓ move, Enter picks, Esc clears. */
export function NodeSearch({
  nodes,
  onSelect,
  onSelectId,
  label = "Search nodes",
  keepResults = false,
}: {
  nodes: MapNode[];
  onSelect: (node: MapNode) => void;
  /** Also accept a node ID absent from the map, for Diagnostics filters. */
  onSelectId?: (nodeId: number) => void;
  label?: string;
  /** Leave the results open after a pick, to try the next one. */
  keepResults?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const index = useMemo(() => nodeSearchIndex(nodes), [nodes]);
  const results = useMemo(() => searchNodes(index, query), [index, query]);
  const typed = query.trim();
  const typedId = /^\d+$/.test(typed)
    ? Number(typed)
    : /^!?[0-9a-f]{8}$/i.test(typed)
      ? parseInt(typed.replace(/^!/, ""), 16)
      : NaN;
  const extraId =
    onSelectId !== undefined &&
    typedId >= 1 &&
    typedId <= 0xffffffff &&
    !results.some((node) => node.node_id === typedId)
      ? typedId
      : null;
  const choices = results.length + (extraId === null ? 0 : 1);
  const listId = useId();
  const open = query.trim() !== "";
  const active = Math.min(activeIndex, choices - 1);

  const choose = (node: MapNode) => {
    if (!keepResults) {
      setQuery("");
      setActiveIndex(0);
    }
    onSelect(node);
  };
  const chooseId = (nodeId: number) => {
    setQuery("");
    setActiveIndex(0);
    onSelectId?.(nodeId);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (choices === 0) return;
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((active + step + choices) % choices);
    } else if (event.key === "Enter" && results[active] !== undefined) {
      event.preventDefault();
      choose(results[active]);
    } else if (event.key === "Enter" && extraId !== null) {
      event.preventDefault();
      chooseId(extraId);
    } else if (event.key === "Escape") {
      setQuery("");
    }
  };

  return (
    <div className="node-search">
      <input
        type="search"
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={
          open && choices > 0 ? `${listId}-${active}` : undefined
        }
        placeholder="Search name or node ID"
        autoComplete="off"
        spellCheck={false}
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setActiveIndex(0);
        }}
        onKeyDown={onKeyDown}
      />
      {open ? (
        choices > 0 ? (
          <ul id={listId} role="listbox" aria-label="Matching nodes">
            {results.map((node, index) => (
              <li
                key={node.node_id}
                id={`${listId}-${index}`}
                role="option"
                aria-selected={index === active}
                // Keep focus in the box so the list stays open while clicking.
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(node)}
                onMouseMove={() => setActiveIndex(index)}
              >
                <span className="node-avatar avatar-green">
                  {nodeMarkerLabel(node)}
                </span>
                <span className="node-search-names">
                  <span>{nodeLabel(node)}</span>
                  <span className="mono muted">
                    {formatNodeId(node.node_id)}
                  </span>
                </span>
              </li>
            ))}
            {extraId !== null ? (
              <li
                id={`${listId}-${results.length}`}
                role="option"
                aria-selected={active === results.length}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseId(extraId)}
                onMouseMove={() => setActiveIndex(results.length)}
              >
                Use node {formatNodeId(extraId)}
              </li>
            ) : null}
          </ul>
        ) : (
          <p id={listId} className="node-search-empty" role="status">
            No matching nodes on the map.
          </p>
        )
      ) : null}
    </div>
  );
}
