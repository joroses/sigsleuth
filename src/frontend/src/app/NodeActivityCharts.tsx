import { PointerEvent, ReactNode, useState } from "react";

import { NodeCharts, NodeUtilization, nodeChartsUrl } from "./api";
import {
  ActivityDay,
  PORT_GROUPS,
  axisTicks,
  firstChartDay,
  lineRuns,
  packetsByDay,
  stackSegments,
} from "./nodeActivity";
import { now as clockNow } from "./clock";
import { useApi } from "./useApi";
import { StatusPanel } from "./ui";

// Both charts share one drawing area, in SVG user units; the SVG scales to fit.
const WIDTH = 640;
const HEIGHT = 200;
const PLOT = { left: 44, right: 12, top: 12, bottom: 28 };
const PLOT_WIDTH = WIDTH - PLOT.left - PLOT.right;
const PLOT_HEIGHT = HEIGHT - PLOT.top - PLOT.bottom;
// Telemetry further apart than this is drawn as a break in the line.
const UTILIZATION_GAP_MS = 3 * 24 * 60 * 60 * 1000;

const dayLabel = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  day: "numeric",
});
const timeLabel = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
});

/** The node page's charts: packets per local day by port, and ChUtil and AirUtil. */
export function NodeActivityCharts({ nodeId }: { nodeId: number }) {
  // Fixed when the page opens, so the request does not change on every render.
  const [{ firstDay, openedAt }] = useState(() => {
    const now = new Date(clockNow());
    return { firstDay: firstChartDay(now), openedAt: now.getTime() };
  });
  const { data, error } = useApi<NodeCharts>(nodeChartsUrl(nodeId, firstDay));
  if (error !== null) return <StatusPanel tone="error">{error}</StatusPanel>;
  if (data === null) return <StatusPanel>Loading activity…</StatusPanel>;
  return (
    <div className="activity-charts">
      <figure className="activity-chart">
        <figcaption>
          <h3>Packets per day</h3>
        </figcaption>
        <PacketChart days={packetsByDay(data.hours, firstDay)} />
      </figure>
      {/* Without the node's own device telemetry there is nothing to draw. */}
      {data.utilization.length > 0 ? (
        <figure className="activity-chart">
          <figcaption>
            <h3>ChUtil and AirUtil</h3>
          </figcaption>
          <UtilizationChart
            points={data.utilization}
            start={firstDay.getTime()}
            end={openedAt}
          />
        </figure>
      ) : null}
    </div>
  );
}

function PacketChart({ days }: { days: ActivityDay[] }) {
  if (days.every((day) => day.total === 0)) {
    return <p className="muted">No packets in the last 7 days.</p>;
  }
  const present = PORT_GROUPS.filter((group) =>
    days.some((day) => day.groups.some((item) => item.group === group)),
  );
  return (
    <>
      <Legend
        items={present.map((group) => ({
          label: group.label,
          color: group.color,
        }))}
      />
      <DayBarChart
        days={days}
        bar={(day, x, width, pixelsPerUnit) => {
          const segments = stackSegments(
            day.groups.map((item) => item.packets),
            pixelsPerUnit,
          );
          return day.groups.map(({ group }, segment) => {
            const { offset, height } = segments[segment];
            return (
              <rect
                key={group.key}
                x={x}
                y={PLOT.top + PLOT_HEIGHT - offset - height}
                width={width}
                height={height}
                fill={group.color}
              />
            );
          });
        }}
        tooltip={(day) => (
          <table>
            <tbody>
              {[...day.groups].reverse().flatMap(({ group, packets, ports }) =>
                // Other lists each of its ports, all in its one colour.
                (group.key === "OTHER"
                  ? ports.map((item) => ({ ...item, label: item.port }))
                  : [{ port: group.key, packets, label: group.label }]
                ).map((row) => (
                  <tr key={row.port}>
                    <td className="number">{row.packets.toLocaleString()}</td>
                    <td>
                      <Swatch color={group.color} /> {row.label}
                    </td>
                  </tr>
                )),
              )}
              <tr>
                <td className="number">
                  <strong>{day.total.toLocaleString()}</strong>
                </td>
                <td>Total</td>
              </tr>
            </tbody>
          </table>
        )}
      />
    </>
  );
}

/**
 * One bar per local day for a single count, such as the Zones page's
 * broadcast texts; `what` names the count in the tooltip.
 */
export function DayTotalChart({
  days,
  what,
  color,
}: {
  days: ActivityDay[];
  what: string;
  color: string;
}) {
  if (days.every((day) => day.total === 0)) {
    return <p className="muted">No {what} in the last 7 days.</p>;
  }
  return (
    <DayBarChart
      days={days}
      bar={(day, x, width, pixelsPerUnit) => {
        const height = day.total * pixelsPerUnit;
        return (
          <rect
            x={x}
            y={PLOT.top + PLOT_HEIGHT - height}
            width={width}
            height={height}
            fill={color}
          />
        );
      }}
      tooltip={(day) => (
        <div>
          {day.total.toLocaleString()} {what}
        </div>
      )}
    />
  );
}

/**
 * A column per day, scaled to the highest total, with the day's tooltip while
 * the pointer is anywhere in its column. `bar` draws a day's bar at `x`;
 * `tooltip` gives what follows the date in its tooltip.
 */
function DayBarChart({
  days,
  bar,
  tooltip,
}: {
  days: ActivityDay[];
  bar: (
    day: ActivityDay,
    x: number,
    width: number,
    pixelsPerUnit: number,
  ) => ReactNode;
  tooltip: (day: ActivityDay) => ReactNode;
}) {
  const [hovered, setHovered] = useState<number | null>(null);
  const ticks = axisTicks(Math.max(...days.map((day) => day.total)));
  const max = ticks[ticks.length - 1];
  const slot = PLOT_WIDTH / days.length;
  const barWidth = Math.min(48, slot * 0.6);
  const y = (value: number) => PLOT.top + PLOT_HEIGHT * (1 - value / max);
  const hoveredDay = hovered === null ? null : days[hovered];
  return (
    <div className="chart-frame">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={days
          .map((day) => `${dayLabel.format(day.start)}: ${day.total}`)
          .join(", ")}
        onPointerLeave={() => setHovered(null)}
      >
        <ValueAxis ticks={ticks} y={y} />
        {days.map((day, index) => {
          const x = PLOT.left + slot * index + (slot - barWidth) / 2;
          return (
            <g
              key={day.start.getTime()}
              className={hovered === index ? "is-hovered" : undefined}
            >
              {bar(day, x, barWidth, PLOT_HEIGHT / max)}
              <text
                className="chart-axis-label"
                x={x + barWidth / 2}
                y={HEIGHT - 8}
                textAnchor="middle"
              >
                {dayLabel.format(day.start)}
              </text>
              {/* The whole column is the hover target, not just the bar. */}
              <rect
                x={PLOT.left + slot * index}
                y={PLOT.top}
                width={slot}
                height={PLOT_HEIGHT}
                fill="transparent"
                onPointerEnter={() => setHovered(index)}
              />
            </g>
          );
        })}
      </svg>
      {hoveredDay !== null && hovered !== null ? (
        <ChartTooltip
          left={((PLOT.left + slot * (hovered + 0.5)) / WIDTH) * 100}
        >
          <strong>{dayLabel.format(hoveredDay.start)}</strong>
          {tooltip(hoveredDay)}
        </ChartTooltip>
      ) : null}
    </div>
  );
}

const UTILIZATION_SERIES = [
  {
    key: "channel_utilization",
    label: "ChUtil",
    color: "var(--series-1)",
  },
  { key: "air_util_tx", label: "AirUtil", color: "var(--series-2)" },
] as const;

function UtilizationChart({
  points,
  start,
  end,
}: {
  points: NodeUtilization[];
  start: number;
  end: number;
}) {
  const [hovered, setHovered] = useState<NodeUtilization | null>(null);
  const values = points.flatMap((point) =>
    UTILIZATION_SERIES.map((series) => point[series.key] ?? 0),
  );
  const ticks = axisTicks(Math.max(...values), 5);
  const max = ticks[ticks.length - 1];
  const x = (at: number) =>
    PLOT.left + (PLOT_WIDTH * (at - start)) / (end - start);
  const y = (value: number) => PLOT.top + PLOT_HEIGHT * (1 - value / max);
  const times = points.map((point) => new Date(point.observed_at).getTime());
  const midnights: Date[] = [];
  for (
    let day = new Date(start);
    day.getTime() < end;
    day = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1)
  ) {
    midnights.push(day);
  }

  function pickNearest(event: PointerEvent<SVGSVGElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    const svgX = ((event.clientX - box.left) / box.width) * WIDTH;
    const at = start + ((svgX - PLOT.left) / PLOT_WIDTH) * (end - start);
    let nearest = 0;
    times.forEach((time, index) => {
      if (Math.abs(time - at) < Math.abs(times[nearest] - at)) nearest = index;
    });
    setHovered(points[nearest]);
  }

  const hoveredAt =
    hovered === null ? null : new Date(hovered.observed_at).getTime();
  return (
    <>
      <Legend items={UTILIZATION_SERIES} />
      <div className="chart-frame">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`ChUtil and AirUtil from ${points.length} telemetry packets, up to ${max}%`}
          onPointerMove={pickNearest}
          onPointerLeave={() => setHovered(null)}
        >
          <ValueAxis ticks={ticks} y={y} unit="%" />
          {midnights.map((day) => (
            <text
              key={day.getTime()}
              className="chart-axis-label"
              x={x(day.getTime())}
              y={HEIGHT - 8}
            >
              {dayLabel.format(day)}
            </text>
          ))}
          {UTILIZATION_SERIES.map((series) => (
            <g key={series.key}>
              {lineRuns(
                times,
                points.map((point) => point[series.key]),
                UTILIZATION_GAP_MS,
              ).map((run) => (
                // Every report gets a dot, joined to its neighbours in the run.
                <g key={run[0].at} fill={series.color}>
                  {run.length > 1 ? (
                    <path
                      d={run
                        .map(
                          (sample, i) =>
                            `${i === 0 ? "M" : "L"}${x(sample.at).toFixed(1)},${y(sample.value).toFixed(1)}`,
                        )
                        .join("")}
                      fill="none"
                      stroke={series.color}
                      strokeWidth={1.5}
                      strokeLinejoin="round"
                    />
                  ) : null}
                  {run.map((sample) => (
                    <circle
                      key={sample.at}
                      cx={x(sample.at)}
                      cy={y(sample.value)}
                      r={2.5}
                    />
                  ))}
                </g>
              ))}
            </g>
          ))}
          {hovered !== null && hoveredAt !== null ? (
            <g>
              <line
                className="chart-crosshair"
                x1={x(hoveredAt)}
                x2={x(hoveredAt)}
                y1={PLOT.top}
                y2={PLOT.top + PLOT_HEIGHT}
              />
              {UTILIZATION_SERIES.map((series) => {
                const value = hovered[series.key];
                return value === null ? null : (
                  <circle
                    key={series.key}
                    className="chart-point"
                    cx={x(hoveredAt)}
                    cy={y(value)}
                    r={4}
                    fill={series.color}
                  />
                );
              })}
            </g>
          ) : null}
        </svg>
        {hovered !== null && hoveredAt !== null ? (
          <ChartTooltip left={(x(hoveredAt) / WIDTH) * 100}>
            <strong>{timeLabel.format(hoveredAt)}</strong>
            <table>
              <tbody>
                {UTILIZATION_SERIES.map((series) => (
                  <tr key={series.key}>
                    <td className="number">
                      {hovered[series.key] === null
                        ? "–"
                        : `${hovered[series.key]?.toFixed(1)}%`}
                    </td>
                    <td>
                      <Swatch color={series.color} /> {series.label}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ChartTooltip>
        ) : null}
      </div>
    </>
  );
}

function ValueAxis({
  ticks,
  y,
  unit = "",
}: {
  ticks: number[];
  y: (value: number) => number;
  unit?: string;
}) {
  return (
    <g>
      {ticks.map((tick) => (
        <g key={tick}>
          <line
            className="chart-grid"
            x1={PLOT.left}
            x2={WIDTH - PLOT.right}
            y1={y(tick)}
            y2={y(tick)}
          />
          <text
            className="chart-axis-label"
            x={PLOT.left - 6}
            y={y(tick) + 4}
            textAnchor="end"
          >
            {tick.toLocaleString()}
            {unit}
          </text>
        </g>
      ))}
    </g>
  );
}

function Legend({
  items,
}: {
  items: readonly { label: string; color: string }[];
}) {
  return (
    <ul className="chart-legend">
      {items.map((item) => (
        <li key={item.label}>
          <Swatch color={item.color} /> {item.label}
        </li>
      ))}
    </ul>
  );
}

function Swatch({ color }: { color: string }) {
  return (
    <span
      className="chart-swatch"
      style={{ background: color }}
      aria-hidden="true"
    />
  );
}

function ChartTooltip({
  left,
  children,
}: {
  left: number;
  children: ReactNode;
}) {
  // Kept inside the chart: centred on the mark, clamped away from the edges.
  return (
    <div
      className="chart-tooltip"
      style={{ left: `${Math.min(80, Math.max(20, left))}%` }}
    >
      {children}
    </div>
  );
}
