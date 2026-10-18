import { NodeChartsHour } from "./api";

export const CHART_DAYS = 7;

/**
 * Chart groups for application ports, in stacking order from the bottom. Each
 * port keeps its colour on every node; the rest, unreadable packets included,
 * are added up as a grey Other.
 */
export const PORT_GROUPS = [
  { key: "TEXT_MESSAGE_APP", label: "Text", color: "var(--series-1)" },
  { key: "POSITION_APP", label: "Position", color: "var(--series-2)" },
  { key: "NODEINFO_APP", label: "Node info", color: "var(--series-3)" },
  { key: "TELEMETRY_APP", label: "Telemetry", color: "var(--series-4)" },
  { key: "TRACEROUTE_APP", label: "Traceroute", color: "var(--series-5)" },
  { key: "OTHER", label: "Other", color: "var(--series-other)" },
] as const;

export type PortGroup = (typeof PORT_GROUPS)[number];

/** The chart group for a port name, or Other. */
export function portGroup(port: string | null): PortGroup {
  return (
    PORT_GROUPS.find((item) => item.key === port) ??
    PORT_GROUPS[PORT_GROUPS.length - 1]
  );
}

export interface ActivityDay {
  start: Date;
  total: number;
  /**
   * Packets per group, in PORT_GROUPS order, groups without packets left out.
   * `ports` names the ports added up in the group, most packets first, so
   * Other can say what it holds.
   */
  groups: {
    group: PortGroup;
    packets: number;
    ports: { port: string; packets: number }[];
  }[];
}

/** Midnight, local time, `days - 1` days before today: the charts' first day. */
export function firstChartDay(now: Date, days = CHART_DAYS): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - days + 1);
}

/**
 * Adds up hourly packets into local days from firstDay. Hours are whole UTC
 * hours, so in a time zone with a half-hour offset a day's edge is off by half
 * an hour; close enough for a chart.
 */
export function packetsByDay(
  hours: NodeChartsHour[],
  firstDay: Date,
  days = CHART_DAYS,
): ActivityDay[] {
  const starts = Array.from(
    { length: days },
    (_, index) =>
      new Date(
        firstDay.getFullYear(),
        firstDay.getMonth(),
        firstDay.getDate() + index,
      ),
  );
  // Packets per day, per group, per port.
  const counts = starts.map(() => new Map<PortGroup, Map<string, number>>());
  for (const hour of hours) {
    const at = new Date(hour.hour_start).getTime();
    const day = starts.findLastIndex((start) => start.getTime() <= at);
    if (day === -1) continue;
    const group = portGroup(hour.port);
    const ports = counts[day].get(group) ?? new Map<string, number>();
    ports.set(hour.port, (ports.get(hour.port) ?? 0) + hour.packets);
    counts[day].set(group, ports);
  }
  return starts.map((start, index) => {
    const groups = PORT_GROUPS.flatMap((group) => {
      const ports = [...(counts[index].get(group) ?? [])]
        .map(([port, packets]) => ({ port, packets }))
        .filter((item) => item.packets > 0)
        .sort((a, b) => b.packets - a.packets);
      const packets = ports.reduce((sum, item) => sum + item.packets, 0);
      return packets > 0 ? [{ group, packets, ports }] : [];
    });
    return {
      start,
      total: groups.reduce((sum, item) => sum + item.packets, 0),
      groups,
    };
  });
}

/**
 * Gridline values for an axis from 0 that reaches value (at least minimum):
 * a round whole step of 1, 2, 2.5 or 5 times a power of ten, at most five of
 * them, ending at the first gridline at or above value. 119 gives 0, 25, 50,
 * 75, 100, 125. The last tick is the axis maximum.
 */
export function axisTicks(value: number, minimum = 1): number[] {
  const target = Math.max(value, minimum);
  const least = target / 5;
  const power = 10 ** Math.max(0, Math.floor(Math.log10(least)));
  const step =
    [1, 2, 2.5, 5, 10]
      .map((factor) => factor * power)
      .find((size) => size >= least && Number.isInteger(size)) ?? 10 * power;
  const parts = Math.ceil(target / step);
  return Array.from({ length: parts + 1 }, (_, index) => index * step);
}

/**
 * Pixel spans for one stacked bar, bottom first: `offset` is the distance of
 * a segment's bottom from the axis. Segments tall enough keep a 2px gap above
 * them; smaller ones skip the gap and are drawn at least 1px tall, so a single
 * packet beside hundreds of others is still visible.
 */
export function stackSegments(
  values: number[],
  pixelsPerUnit: number,
): { offset: number; height: number }[] {
  let base = 0;
  return values.map((value) => {
    const offset = base * pixelsPerUnit;
    const full = value * pixelsPerUnit;
    base += value;
    const height = full > 4 ? full - 2 : Math.max(1, full);
    return { offset, height };
  });
}

/**
 * Splits a time series into runs to draw as connected lines, leaving out
 * missing values and breaking wherever samples are more than `gapMs` apart.
 * A run of one sample has no line and needs a marker to be seen.
 */
export function lineRuns(
  times: number[],
  values: (number | null)[],
  gapMs: number,
): { at: number; value: number }[][] {
  const runs: { at: number; value: number }[][] = [];
  let previous: number | null = null;
  values.forEach((value, index) => {
    if (value === null) return;
    const at = times[index];
    if (previous === null || at - previous > gapMs) runs.push([]);
    runs[runs.length - 1].push({ at, value });
    previous = at;
  });
  return runs;
}
