import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CoverageNodeList, CoverageViewNode, MapNode } from "./api";
import { HeardInView } from "./HeardInView";

afterEach(cleanup);

const heard = (node_id: number, long_name: string): CoverageViewNode => ({
  node_id,
  long_name,
  short_name: null,
  cells: 1,
  measurements: 1,
  average_margin: 10,
});

const page: CoverageNodeList = {
  nodes: [
    heard(3, "Third"),
    heard(1, "First"),
    heard(2, "Unplaced"),
    heard(4, "Filtered"),
  ],
  unknown: 4,
  truncated: false,
};

const mapNode = (node_id: number) => ({ node_id }) as MapNode;

describe("HeardInView", () => {
  it("keeps the server's order and chooses unplaced nodes but not filtered nodes", () => {
    const onChoose = vi.fn();
    render(
      <HeardInView
        page={page}
        error={null}
        selectedNodeId={1}
        drawn={new Map([1, 3].map((id) => [id, mapNode(id)]))}
        loadedIds={new Set([1, 3, 4])}
        onChoose={onChoose}
      />,
    );
    const list = screen.getByRole("region", { name: "Coverage" });
    const rows = within(list).getAllByRole("button", {
      name: /Third|First|Unplaced|Filtered/,
    });
    expect(rows.map((row) => row.textContent)).toEqual([
      "Third1",
      "First1",
      "Unplaced1",
      "Filtered1",
    ]);
    expect(rows[1].getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(rows[0]);
    fireEvent.click(rows[2]);
    fireEvent.click(rows[3]);
    expect(onChoose.mock.calls).toEqual([
      [heard(3, "Third")],
      [heard(2, "Unplaced")],
    ]);
  });
});
