import { KeyboardEvent, useId, useMemo, useState } from "react";

import { formatNodeId, nodeLabel, nodeMarkerLabel } from "./nodeDisplay";
import { nodeSearchIndex, SearchableNode, searchNodes } from "./nodeSearch";

/**
 * A search box over the given nodes; ↑/↓ move, Enter picks, Esc clears.
 * The results drop down over the page below and close when the box loses
 * focus.
 */
export function NodeSearch<T extends SearchableNode>({
  nodes,
  onSelect,
  onSelectId,
  onQuery,
  emptyText = "No matching nodes on the map.",
  label = "Search nodes",
  keepResults = false,
  placeholder = "Search name or node ID",
  autoFocus = false,
}: {
  nodes: readonly T[];
  onSelect: (node: T) => void;
  /** Also accept a node ID absent from the nodes, for the node filters. */
  onSelectId?: (nodeId: number) => void;
  /** Told what is typed, by a search that loads its nodes for it. */
  onQuery?: (query: string) => void;
  /** Shown when nothing matches; null shows nothing, as while loading. */
  emptyText?: string | null;
  label?: string;
  /** Keep the query after a pick; a click in the box shows the results again. */
  keepResults?: boolean;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  const [query, setQueryText] = useState("");
  const setQuery = (text: string) => {
    setQueryText(text);
    onQuery?.(text);
  };
  const [activeIndex, setActiveIndex] = useState(0);
  const [dismissed, setDismissed] = useState(false);
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
  const open = typed !== "" && !dismissed;
  const active = Math.min(activeIndex, choices - 1);

  const choose = (node: T) => {
    if (keepResults) setDismissed(true);
    else {
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
      if (!open) {
        setDismissed(false);
        return;
      }
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((active + step + choices) % choices);
    } else if (event.key === "Escape") {
      setQuery("");
    } else if (event.key !== "Enter" || !open) {
      return;
    } else if (results[active] !== undefined) {
      event.preventDefault();
      choose(results[active]);
    } else if (extraId !== null) {
      event.preventDefault();
      chooseId(extraId);
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
        placeholder={placeholder}
        autoFocus={autoFocus}
        autoComplete="off"
        spellCheck={false}
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setActiveIndex(0);
          setDismissed(false);
        }}
        onFocus={() => setDismissed(false)}
        onClick={() => setDismissed(false)}
        onBlur={() => setDismissed(true)}
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
                <span className="node-search-name">{nodeLabel(node)}</span>
                <span className="mono muted">{formatNodeId(node.node_id)}</span>
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
        ) : emptyText !== null ? (
          <p id={listId} className="node-search-empty" role="status">
            {emptyText}
          </p>
        ) : null
      ) : null}
    </div>
  );
}
