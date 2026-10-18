import { describe, expect, it } from "vitest";

import { NodeChartsHour } from "./api";
import {
  firstChartDay,
  lineRuns,
  packetsByDay,
  stackSegments,
} from "./nodeActivity";

function hour(at: Date, port: string, packets: number): NodeChartsHour {
  return { hour_start: at.toISOString(), portnum: null, port, packets };
}

describe("packetsByDay", () => {
  const firstDay = firstChartDay(new Date(2026, 8, 29, 15, 0));

  it("adds hours into local days by port group, unknown ports as Other", () => {
    const days = packetsByDay(
      [
        hour(new Date(2026, 8, 22, 23, 0), "TEXT_MESSAGE_APP", 9), // before the chart
        hour(new Date(2026, 8, 23, 0, 0), "TEXT_MESSAGE_APP", 2),
        hour(new Date(2026, 8, 23, 23, 0), "POSITION_APP", 1),
        hour(new Date(2026, 8, 29, 14, 0), "RANGE_TEST_APP", 4),
        hour(new Date(2026, 8, 29, 14, 0), "PAXCOUNTER_APP", 1),
      ],
      firstDay,
    );

    expect(days).toHaveLength(7);
    expect(days[0].start).toEqual(new Date(2026, 8, 23));
    expect(days[0].total).toBe(3);
    expect(days[0].groups.map((item) => item.group.label)).toEqual([
      "Text",
      "Position",
    ]);
    expect(days[3].groups).toEqual([]);
    expect(
      days[6].groups.map((item) => [item.group.label, item.packets]),
    ).toEqual([["Other", 5]]);
    expect(days[6].groups[0].ports).toEqual([
      { port: "RANGE_TEST_APP", packets: 4 },
      { port: "PAXCOUNTER_APP", packets: 1 },
    ]);
  });
});

describe("stackSegments", () => {
  it("keeps a single packet visible beside a hundred others", () => {
    // 164px for an axis of 200 packets: one packet is 0.82px.
    const [text] = stackSegments([1, 100], 164 / 200);

    expect(text.height).toBeGreaterThanOrEqual(1);
  });
});

describe("lineRuns", () => {
  const hourMs = 60 * 60 * 1000;

  it("breaks at long gaps and missing values, leaving lone samples", () => {
    const runs = lineRuns(
      [0, hourMs, 2 * hourMs, 10 * hourMs],
      [5, 6, null, 7],
      3 * hourMs,
    );

    expect(runs.map((run) => run.map((sample) => sample.value))).toEqual([
      [5, 6],
      [7],
    ]);
  });
});
