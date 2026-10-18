import { type KeyboardEvent, useMemo } from "react";

import {
  type GroupLine,
  type ZoneGraph as Graph,
  type ZoneGroup,
  PATTERN_LABELS,
  ZONE_GREY,
  formatShare,
  groupCaption,
  lineColor,
} from "./zones";

// The Zones page's graph view: each island's groups of zones (_groups in
// zones.py) as boxes, joined by lines that stand for the weak connections between
// them, styled as the map's zone lines (LinePattern) and coloured and labelled
// by text reach (lineColor). Each island is a tree from its biggest group, taking
// lines with a solid link before weak ones; other lines are drawn across it.
// Groups in a row go west to east, as on the map. ZonesView's GraphLegend
// explains the marks.

// Sizes in pixels: a zone's box, the gap between boxes, zones per row in a
// group, a group's padding (sides, top with its caption, bottom), the gaps
// between groups side by side and between rows, the drawing's margin and how
// far a line between groups of one row dips below them.
const ZONE = 24;
const GAP = 4;
const PER_ROW = 8;
const PAD_X = 10;
const PAD_TOP = 22;
const PAD_BOTTOM = 18;
const H_GAP = 20;
const V_GAP = 60;
const MARGIN = 12;
const DIP = 30;
// A line's text reach label: its height and padding each side.
const LABEL_H = 16;
const LABEL_PAD = 5;

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
  depth: number;
}

interface IslandLayout {
  island: number;
  groups: ZoneGroup[];
  lines: GroupLine[];
  boxes: Map<number, Box>;
  // Each zone's box centre.
  zones: Map<number, [number, number]>;
  width: number;
  height: number;
  nodes: number;
  zoneCount: number;
}

let measure: CanvasRenderingContext2D | null | undefined;

function textWidth(text: string): number {
  try {
    measure ??= document.createElement("canvas").getContext("2d");
  } catch {
    measure = null;
  }
  if (!measure) return text.length * 6.5;
  measure.font = '500 11px Inter, "Segoe UI", system-ui, sans-serif';
  return measure.measureText(text).width;
}

/** The note under a group: its text reach inside, when it has a figure. */
function textsNote(group: ZoneGroup): string | null {
  if (group.textsInside === null) return null;
  const [low, high] = group.textsInside.map(formatShare);
  return low === high ? `Text reach ${low}` : `Text reach ${low}–${high}`;
}

function layoutIsland(
  island: number,
  groups: ZoneGroup[],
  lines: GroupLine[],
  names: Map<number, string>,
): IslandLayout {
  const byKey = new Map(groups.map((group) => [group.key, group]));
  const root = groups[0];
  const depth = new Map([[root.key, 0]]);
  const children = new Map<number, number[]>(
    groups.map((group) => [group.key, []]),
  );
  // Grow the tree one line at a time: lines with a solid link first, then
  // from the shallowest group, then to the biggest.
  for (;;) {
    let best: { rank: number[]; from: number; to: number } | null = null;
    for (const line of lines) {
      for (const [from, to] of [
        [line.a, line.b],
        [line.b, line.a],
      ]) {
        if (!depth.has(from) || depth.has(to)) continue;
        const rank = [
          line.pattern === "weak" ? 1 : 0,
          depth.get(from) ?? 0,
          -(byKey.get(to)?.nodes ?? 0),
        ];
        if (best === null || compareRanks(rank, best.rank) < 0) {
          best = { rank, from, to };
        }
      }
    }
    if (best === null) break;
    depth.set(best.to, (depth.get(best.from) ?? 0) + 1);
    children.get(best.from)?.push(best.to);
  }
  const west = (key: number) => byKey.get(key)?.longitude ?? Infinity;
  for (const keys of children.values()) keys.sort((a, b) => west(a) - west(b));

  const size = new Map(
    groups.map((group) => {
      const perRow = Math.min(group.zones.length, PER_ROW);
      const rows = Math.ceil(group.zones.length / PER_ROW);
      const note = textsNote(group);
      const w = Math.ceil(
        Math.max(
          perRow * ZONE + (perRow - 1) * GAP,
          textWidth(groupCaption(group, names)),
          note === null ? 0 : textWidth(note),
        ) +
          2 * PAD_X,
      );
      const h =
        PAD_TOP +
        rows * ZONE +
        (rows - 1) * GAP +
        (note === null ? GAP * 2 : PAD_BOTTOM);
      return [group.key, { w, h }];
    }),
  );
  const span = new Map<number, number>();
  const childrenWidth = (key: number) =>
    (children.get(key) ?? []).reduce(
      (sum, child) => sum + (span.get(child) ?? 0),
      0,
    ) +
    H_GAP * Math.max(0, (children.get(key)?.length ?? 0) - 1);
  const measureSpan = (key: number) => {
    for (const child of children.get(key) ?? []) measureSpan(child);
    span.set(key, Math.max(size.get(key)?.w ?? 0, childrenWidth(key)));
  };
  measureSpan(root.key);

  const rowHeight: number[] = [];
  for (const [key, row] of depth) {
    rowHeight[row] = Math.max(rowHeight[row] ?? 0, size.get(key)?.h ?? 0);
  }
  const rowTop: number[] = [];
  let y = MARGIN;
  for (const [row, height] of rowHeight.entries()) {
    rowTop[row] = y;
    y += height + V_GAP;
  }

  const boxes = new Map<number, Box>();
  const place = (key: number, left: number) => {
    const width = span.get(key) ?? 0;
    const { w, h } = size.get(key) ?? { w: 0, h: 0 };
    const row = depth.get(key) ?? 0;
    boxes.set(key, {
      x: left + (width - w) / 2,
      y: rowTop[row],
      w,
      h,
      depth: row,
    });
    let x = left + (width - childrenWidth(key)) / 2;
    for (const child of children.get(key) ?? []) {
      place(child, x);
      x += (span.get(child) ?? 0) + H_GAP;
    }
  };
  place(root.key, MARGIN);

  const zones = new Map<number, [number, number]>();
  for (const group of groups) {
    const box = boxes.get(group.key);
    if (box === undefined) continue;
    group.zones.forEach((zoneId, index) => {
      const row = Math.floor(index / PER_ROW);
      const inRow = Math.min(PER_ROW, group.zones.length - row * PER_ROW);
      const rowWidth = inRow * ZONE + (inRow - 1) * GAP;
      zones.set(zoneId, [
        box.x +
          (box.w - rowWidth) / 2 +
          (index % PER_ROW) * (ZONE + GAP) +
          ZONE / 2,
        box.y + PAD_TOP + row * (ZONE + GAP) + ZONE / 2,
      ]);
    });
  }

  const last = rowHeight.length - 1;
  const dipsBelowLast = lines.some(
    (line) =>
      boxes.get(line.a)?.depth === last && boxes.get(line.b)?.depth === last,
  );
  return {
    island,
    groups,
    lines,
    boxes,
    zones,
    width: (span.get(root.key) ?? 0) + 2 * MARGIN,
    height: rowTop[last] + rowHeight[last] + MARGIN + (dipsBelowLast ? DIP : 0),
    nodes: groups.reduce((sum, group) => sum + group.nodes, 0),
    zoneCount: groups.reduce((sum, group) => sum + group.zones.length, 0),
  };
}

function compareRanks(x: number[], y: number[]): number {
  for (const [index, value] of x.entries()) {
    if (value !== y[index]) return value - y[index];
  }
  return 0;
}

/**
 * The pair a line is drawn between: the one with the most links. The line
 * runs from that zone in the upper group to the one in the lower group, or
 * between groups of one row, dips below both.
 */
function linePath(line: GroupLine, layout: IslandLayout) {
  const carrier = [...line.pairs].sort(
    (x, y) => y.links.length - x.links.length,
  )[0];
  const boxOf = (zoneId: number) =>
    layout.boxes.get(
      layout.groups.find((group) => group.zones.includes(zoneId))?.key ?? -1,
    );
  const [p, q] = [carrier.zone_a, carrier.zone_b];
  let [pb, qb] = [boxOf(p), boxOf(q)];
  let [pc, qc] = [layout.zones.get(p), layout.zones.get(q)];
  if (!pb || !qb || !pc || !qc) return null;
  if (pb.depth > qb.depth || (pb.depth === qb.depth && pc[0] > qc[0])) {
    [pb, qb, pc, qc] = [qb, pb, qc, pc];
  }
  const start: Point = [pc[0], pb.y + pb.h];
  // A cubic curve's four points: start, two controls, end.
  let curve: Point[];
  if (pb.depth === qb.depth) {
    const end: Point = [qc[0], qb.y + qb.h];
    curve = [start, [start[0], start[1] + DIP], [end[0], end[1] + DIP], end];
  } else {
    const end: Point = [qc[0], qb.y];
    const half = (end[1] - start[1]) / 2;
    curve = [start, [start[0], start[1] + half], [end[0], end[1] - half], end];
  }
  const [a, b, c, d] = curve;
  return {
    d: `M${a[0]} ${a[1]} C${b[0]} ${b[1]} ${c[0]} ${c[1]} ${d[0]} ${d[1]}`,
    curve,
  };
}

type Point = [number, number];

/** The point a share t (0 to 1) of the way along a cubic curve. */
function pointAt([a, b, c, d]: Point[], t: number): Point {
  const u = 1 - t;
  const at = (axis: 0 | 1) =>
    u * u * u * a[axis] +
    3 * u * u * t * b[axis] +
    3 * u * t * t * c[axis] +
    t * t * t * d[axis];
  return [at(0), at(1)];
}

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

function overlaps(p: Rect, q: Rect): boolean {
  return (
    p.x < q.x + q.w + 2 &&
    q.x < p.x + p.w + 2 &&
    p.y < q.y + q.h + 2 &&
    q.y < p.y + p.h + 2
  );
}

// Where along its curve a line's label may go, tried in turn.
const LABEL_SPOTS = [0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.12, 0.88];

/**
 * Each line's path and its label: halfway along the curve when that is clear,
 * else the first clear spot of LABEL_SPOTS, so the labels of lines leaving
 * one group cover neither each other nor a group. When none is clear, the
 * spot that covers the fewest, a group counting as many labels.
 */
interface DrawnLine {
  line: GroupLine;
  d: string;
  label: (Rect & { text: string }) | null;
}

function drawnLines(layout: IslandLayout): DrawnLine[] {
  const boxes = [...layout.boxes.values()];
  const labels: Rect[] = [];
  const crowding = (rect: Rect) =>
    10 * boxes.filter((box) => overlaps(rect, box)).length +
    labels.filter((other) => overlaps(rect, other)).length;
  return layout.lines.flatMap((line): DrawnLine[] => {
    const path = linePath(line, layout);
    if (path === null) return [];
    const text = lineLabel(line);
    if (text === null) return [{ line, d: path.d, label: null }];
    const w = textWidth(text) + 2 * LABEL_PAD;
    const rects = LABEL_SPOTS.map((t) => {
      const [x, y] = pointAt(path.curve, t);
      return { x: x - w / 2, y: y - LABEL_H / 2, w, h: LABEL_H };
    });
    const label = rects.reduce((best, rect) =>
      crowding(rect) < crowding(best) ? rect : best,
    );
    labels.push(label);
    return [{ line, d: path.d, label: { ...label, text } }];
  });
}

// A line's stroke class (styles.css): wide, thin, or thin and dashed.
const STROKES: Record<GroupLine["pattern"], string> = {
  solid: "wide",
  "one solid link": "thin",
  weak: "dashed",
};

type Target = { group: number } | { line: string };

/**
 * A line's label: its worst and best zone pairs' text reach, such as
 * "12%–43%"; null with no figure.
 */
function lineLabel(line: GroupLine): string | null {
  if (line.texts === null) return null;
  const best = formatShare(line.texts);
  const worst = formatShare(line.worstTexts ?? line.texts);
  return worst === best ? best : `${worst}–${best}`;
}

function activate(event: KeyboardEvent, select: () => void) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  select();
}

export function ZoneGraph({
  graph,
  colors,
  names,
  selectedGroup,
  selectedLine,
  selectedZone,
  onSelectGroup,
  onSelectLine,
  onSelectZone,
}: {
  graph: Graph;
  colors: Map<number, string>;
  names: Map<number, string>;
  selectedGroup: number | null;
  selectedLine: string | null;
  selectedZone: number | null;
  onSelectGroup: (key: number | null) => void;
  onSelectLine: (key: string | null) => void;
  onSelectZone: (zoneId: number) => void;
}) {
  const layouts = useMemo(() => {
    const islands = new Map<number, ZoneGroup[]>();
    for (const group of graph.groups) {
      islands.set(group.island, [...(islands.get(group.island) ?? []), group]);
    }
    return [...islands.entries()]
      .filter(
        ([, groups]) =>
          groups.some((group) => group.zones.length > 1) || groups.length > 1,
      )
      .map(([island, groups]) => {
        const keys = new Set(groups.map((group) => group.key));
        const layout = layoutIsland(
          island,
          groups,
          graph.lines.filter((line) => keys.has(line.a)),
          names,
        );
        return { ...layout, drawn: drawnLines(layout) };
      });
  }, [graph, names]);
  const alone = graph.groups.filter(
    (group) =>
      group.zones.length === 1 &&
      !graph.groups.some(
        (other) => other.island === group.island && other !== group,
      ),
  );
  // Zones that hang on a single solid link inside their group, with the
  // pair they hang on.
  const hanging = useMemo(() => {
    const marks = new Map<number, string>();
    for (const group of graph.groups) {
      for (const pair of group.singleLinks) {
        for (const zoneId of pair.hanging_zones) {
          marks.set(
            zoneId,
            `Hangs on one solid link: ${names.get(pair.zone_a)}–${names.get(pair.zone_b)}`,
          );
        }
      }
    }
    return marks;
  }, [graph, names]);

  // What a selected group or line lights up: itself and what it touches; the
  // rest dims. Hovering only marks the item under the pointer (styles.css).
  const focus: Target | null =
    selectedGroup !== null
      ? { group: selectedGroup }
      : selectedLine !== null
        ? { line: selectedLine }
        : null;
  const lit = new Set<string>();
  if (focus !== null && "group" in focus) {
    lit.add(`g${focus.group}`);
    for (const line of graph.lines) {
      if (line.a === focus.group || line.b === focus.group) {
        lit.add(`l${line.key}`).add(`g${line.a}`).add(`g${line.b}`);
      }
    }
  } else if (focus !== null) {
    const line = graph.lines.find((item) => item.key === focus.line);
    if (line) lit.add(`l${line.key}`).add(`g${line.a}`).add(`g${line.b}`);
  }
  const dim = (id: string) => (focus !== null && !lit.has(id) ? " dim" : "");
  const lineClass = (line: GroupLine) =>
    `zones-graph-line${line.key === selectedLine ? " selected" : ""}${dim(`l${line.key}`)}`;
  const toggleLine = (line: GroupLine) =>
    onSelectLine(line.key === selectedLine ? null : line.key);

  if (layouts.length === 0 && alone.length === 0) {
    return <p className="empty-inline">No zones to draw.</p>;
  }
  return (
    <div className="zones-graph">
      {layouts.map((layout, index) => (
        <section key={layout.island} className="zones-graph-island">
          <h4>
            Island {index + 1}{" "}
            <small>
              {layout.zoneCount} zones · {layout.groups.length} group
              {layout.groups.length === 1 ? "" : "s"} ·{" "}
              {layout.nodes.toLocaleString()} nodes
            </small>
          </h4>
          <div className="zones-graph-scroll">
            <svg
              width={layout.width}
              height={layout.height}
              viewBox={`0 0 ${layout.width} ${layout.height}`}
              role="group"
              aria-label={`Island ${index + 1}`}
            >
              <g>
                {layout.drawn.map(({ line, d, label }) => {
                  const text = label?.text ?? "too few texts";
                  const name = `${groupCaption(groupOf(graph, line.a), names)} and ${groupCaption(groupOf(graph, line.b), names)}: ${PATTERN_LABELS[line.pattern]}, ${text}`;
                  return (
                    <g
                      key={line.key}
                      className={lineClass(line)}
                      tabIndex={0}
                      role="button"
                      aria-label={name}
                      aria-pressed={line.key === selectedLine}
                      onClick={() => toggleLine(line)}
                      onKeyDown={(event) =>
                        activate(event, () => toggleLine(line))
                      }
                    >
                      <path className="zones-graph-hit" d={d} />
                      <path
                        className={`zones-graph-stroke ${STROKES[line.pattern]}`}
                        d={d}
                        stroke={lineColor(line)}
                      />
                    </g>
                  );
                })}
              </g>
              {/* Labels above every line, so no line crosses one. */}
              <g aria-hidden="true">
                {layout.drawn.map(({ line, label }) =>
                  label === null ? null : (
                    <g
                      key={line.key}
                      className={`${lineClass(line)} zones-graph-label`}
                      onClick={() => toggleLine(line)}
                    >
                      <rect
                        x={label.x}
                        y={label.y}
                        width={label.w}
                        height={label.h}
                        rx={label.h / 2}
                        fill={lineColor(line)}
                      />
                      <text
                        x={label.x + label.w / 2}
                        y={label.y + label.h / 2 + 4}
                        textAnchor="middle"
                      >
                        {label.text}
                      </text>
                    </g>
                  ),
                )}
              </g>
              <g>
                {layout.groups.map((group) => {
                  const box = layout.boxes.get(group.key);
                  if (box === undefined) return null;
                  const note = textsNote(group);
                  const label = `${groupCaption(group, names)}: ${group.zones.length} zones, ${group.nodes} nodes${note === null ? "" : `, ${note}`}`;
                  return (
                    <g
                      key={group.key}
                      className={`zones-graph-group${group.key === selectedGroup ? " selected" : ""}${dim(`g${group.key}`)}`}
                      tabIndex={0}
                      role="button"
                      aria-label={label}
                      aria-pressed={group.key === selectedGroup}
                      onClick={() =>
                        onSelectGroup(
                          group.key === selectedGroup ? null : group.key,
                        )
                      }
                      onKeyDown={(event) =>
                        activate(event, () =>
                          onSelectGroup(
                            group.key === selectedGroup ? null : group.key,
                          ),
                        )
                      }
                    >
                      <title>
                        {group.zones
                          .map(
                            (zoneId) =>
                              `${zoneId}. ${names.get(zoneId)}${hanging.has(zoneId) ? ` · ${hanging.get(zoneId)}` : ""}`,
                          )
                          .join("\n")}
                      </title>
                      <rect
                        className="zones-graph-box"
                        x={box.x}
                        y={box.y}
                        width={box.w}
                        height={box.h}
                        rx={8}
                      />
                      <text
                        className="zones-graph-caption"
                        x={box.x + box.w / 2}
                        y={box.y + 15}
                        textAnchor="middle"
                      >
                        {groupCaption(group, names)}
                      </text>
                      {group.zones.map((zoneId) => {
                        const [cx, cy] = layout.zones.get(zoneId) ?? [0, 0];
                        return (
                          <g
                            key={zoneId}
                            className={
                              zoneId === selectedZone
                                ? "zones-graph-zone selected"
                                : "zones-graph-zone"
                            }
                            onClick={(event) => {
                              event.stopPropagation();
                              onSelectZone(zoneId);
                            }}
                          >
                            <rect
                              x={cx - ZONE / 2}
                              y={cy - ZONE / 2}
                              width={ZONE}
                              height={ZONE}
                              rx={4}
                              fill={colors.get(zoneId) ?? ZONE_GREY}
                            />
                            <text x={cx} y={cy + 4} textAnchor="middle">
                              {zoneId}
                            </text>
                          </g>
                        );
                      })}
                      {note !== null ? (
                        <text
                          className="zones-graph-note"
                          x={box.x + box.w / 2}
                          y={box.y + box.h - 5}
                          textAnchor="middle"
                        >
                          {note}
                        </text>
                      ) : null}
                    </g>
                  );
                })}
              </g>
            </svg>
          </div>
        </section>
      ))}
      {alone.length > 0 ? (
        <section className="zones-graph-island">
          <h4>
            Alone{" "}
            <small>{alone.length} zones with no link to another zone</small>
          </h4>
          <ul className="zones-graph-alone">
            {alone.map((group) => (
              <li key={group.key}>
                <button
                  type="button"
                  className="zones-zone-chip"
                  onClick={() => onSelectZone(group.key)}
                >
                  <span
                    className="zones-graph-alone-zone"
                    style={{ background: colors.get(group.key) ?? ZONE_GREY }}
                  >
                    {group.key}
                  </span>
                  {names.get(group.key)}{" "}
                  <span className="muted">{group.nodes} nodes</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function groupOf(graph: Graph, key: number): ZoneGroup {
  return graph.groups.find((group) => group.key === key) ?? graph.groups[0];
}
