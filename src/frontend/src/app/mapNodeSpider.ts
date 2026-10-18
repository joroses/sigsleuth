// Markers at one spot spread into a ring when clicked at high zoom, so each
// can be picked. The ring is one DOM element anchored at the spot, so it
// follows pans; legs and dots keep their screen size.
import type { MapNode } from "./api";
import { NODE_COLORS, nodeCategory } from "./mapNodeLayers";
import { formatNodeId, nodeLabel, nodeMarkerLabel } from "./nodeDisplay";

/** Below this zoom a click on overlapping markers zooms in instead. */
export const SPIDER_MIN_ZOOM = 12;
/** Dots whose centres are this close overlap at SPIDER_MIN_ZOOM, rims included. */
export const SPIDER_OVERLAP_PX = 20;

// Dots on the ring sit this far apart, centre to centre, in pixels.
const SPACING = 40;
const MIN_RADIUS = 34;

export function spiderElement(
  nodes: MapNode[],
  onChoose: (node: MapNode) => void,
): HTMLElement {
  const radius = Math.max(MIN_RADIUS, (nodes.length * SPACING) / (2 * Math.PI));
  const element = document.createElement("div");
  element.className = "node-spider";
  element.setAttribute("role", "group");
  element.setAttribute("aria-label", `${nodes.length} nodes at this spot`);
  // Covers the map's names under the ring; hiding them instead makes the map
  // place every name again, a visible stall on each open and close.
  const backdrop = document.createElement("div");
  backdrop.className = "node-spider-backdrop";
  backdrop.style.width = backdrop.style.height = `${2 * radius + 28}px`;
  element.append(backdrop);
  const svgNs = "http://www.w3.org/2000/svg";
  const legs = document.createElementNS(svgNs, "svg");
  legs.setAttribute("class", "node-spider-legs");
  const size = 2 * radius + 4;
  legs.setAttribute("width", String(size));
  legs.setAttribute("height", String(size));
  legs.setAttribute("viewBox", `${-size / 2} ${-size / 2} ${size} ${size}`);
  element.append(legs);
  const sorted = [...nodes].sort(
    (a, b) => nodeLabel(a).localeCompare(nodeLabel(b)) || a.node_id - b.node_id,
  );
  sorted.forEach((node, index) => {
    // Start at the top and go clockwise.
    const angle = (index / sorted.length) * 2 * Math.PI - Math.PI / 2;
    const x = radius * Math.cos(angle);
    const y = radius * Math.sin(angle);
    const leg = document.createElementNS(svgNs, "line");
    leg.setAttribute("x1", "0");
    leg.setAttribute("y1", "0");
    leg.setAttribute("x2", x.toFixed(1));
    leg.setAttribute("y2", y.toFixed(1));
    legs.append(leg);
    const button = document.createElement("button");
    button.type = "button";
    // The name goes outward from the ring, clear of the neighbouring dots.
    const side =
      Math.abs(x) >= Math.abs(y)
        ? x < 0
          ? "left"
          : "right"
        : y < 0
          ? "up"
          : "down";
    button.className = `node-spider-leaf ${side}`;
    button.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    button.title = `${nodeLabel(node)} ${formatNodeId(node.node_id)}`;
    const dot = document.createElement("span");
    dot.className = "node-spider-dot";
    const color = NODE_COLORS[nodeCategory(node)];
    // An estimated node is hollow, as on the map.
    if (node.estimate === null) dot.style.background = color;
    else dot.style.borderColor = color;
    const label = document.createElement("span");
    label.className = "node-spider-label";
    label.textContent = nodeMarkerLabel(node);
    button.append(dot, label);
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      onChoose(node);
    });
    element.append(button);
  });
  return element;
}
