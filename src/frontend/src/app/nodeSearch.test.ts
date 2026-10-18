import { describe, expect, it } from "vitest";
import { nodeSearchIndex, searchNodes as search } from "./nodeSearch";

const node = (
  node_id: number,
  long_name: string | null,
  short_name: string | null = null,
) => ({ node_id, long_name, short_name });

const searchNodes = (nodes: ReturnType<typeof node>[], query: string) =>
  search(nodeSearchIndex(nodes), query);

const ids = (nodes: { node_id: number }[]) => nodes.map((item) => item.node_id);

describe("searchNodes", () => {
  const nodes = [
    node(0xa1b2c3d4, "Rooftop Relay", "ROOF"),
    node(0x0000beef, "José's base", "JB"),
    node(1234, null, null),
  ];

  it("finds names regardless of case and accents", () => {
    expect(ids(searchNodes(nodes, "relay"))).toEqual([0xa1b2c3d4]);
    expect(ids(searchNodes(nodes, "roof"))).toEqual([0xa1b2c3d4]);
    expect(ids(searchNodes(nodes, "jose"))).toEqual([0x0000beef]);
  });

  it("finds hex node IDs with or without the !, whole or partial", () => {
    expect(ids(searchNodes(nodes, "!a1b2c3d4"))).toEqual([0xa1b2c3d4]);
    expect(ids(searchNodes(nodes, "A1B2C3D4"))).toEqual([0xa1b2c3d4]);
    expect(ids(searchNodes(nodes, "c3d4"))).toEqual([0xa1b2c3d4]);
    expect(ids(searchNodes(nodes, "!0000beef"))).toEqual([0x0000beef]);
  });

  it("finds decimal node numbers", () => {
    expect(ids(searchNodes(nodes, "1234"))).toEqual([1234]);
    expect(ids(searchNodes(nodes, String(0xa1b2c3d4)))).toEqual([0xa1b2c3d4]);
  });

  it("puts exact matches before prefixes before other matches", () => {
    const ranked = [
      node(1, "North base"),
      node(2, "Base camp"),
      node(3, null, "base"),
    ];
    expect(ids(searchNodes(ranked, "base"))).toEqual([3, 2, 1]);
  });

  it("returns at most the limit, keeping input order among equals", () => {
    const many = Array.from({ length: 15 }, (_, index) =>
      node(index + 1, `Relay ${index + 1}`),
    );
    expect(ids(searchNodes(many, "relay"))).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ]);
  });

  it("returns nothing for a blank or unmatched query", () => {
    expect(searchNodes(nodes, "   ")).toEqual([]);
    expect(searchNodes(nodes, "nowhere")).toEqual([]);
  });
});
