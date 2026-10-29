import type { RelayAnswer, RelayCheck, RelayCandidate } from "./api";
import { formatNodeId } from "./nodeDisplay";

// Words relay checks and survey checks share.

export type How = NonNullable<RelayAnswer["how"]>;
export type CheckNode = RelayCheck["sender"];

export const HOW_LABELS: Record<How, string> = {
  heard_directly: "Heard directly",
  receiver_heard: "Heard by the gateway",
  gateway_link: "Links with co-hearers",
  sender_link: "Links with the sender",
  reported: "Reported one hop earlier",
  only_candidate: "Distance",
};

export const WHY_LABELS: Record<
  Exclude<RelayAnswer["why"], "heard_directly" | "named">,
  string
> = {
  below: "Likeliest not sure enough",
  unknown: "An unknown node likeliest",
  no_evidence: "Likeliest has no evidence",
  no_candidates: "No candidate",
};

export function percent(probability: number | null): string {
  if (probability === null) return "—";
  const value = probability * 100;
  return `${value < 1 && value > 0 ? "<1" : Math.round(value)}%`;
}

export function nodeText(node: CheckNode | null): string {
  if (node === null) return "—";
  if (node.node_id === null) return "an opted-out node";
  return node.name ?? formatNodeId(node.node_id);
}

export function byteText(byte: number): string {
  return byte.toString(16).padStart(2, "0");
}

export function hopsText(start: number | null, left: number | null): string {
  if (start === null || left === null) return "hops unknown";
  const used = start - left;
  return `${used} hop${used === 1 ? "" : "s"}`;
}

export function candidateName(candidate: RelayCandidate | undefined): string {
  if (candidate === undefined) return "—";
  if (candidate.node_id === null) return "an opted-out node";
  return (
    candidate.name ?? candidate.short_name ?? formatNodeId(candidate.node_id)
  );
}
