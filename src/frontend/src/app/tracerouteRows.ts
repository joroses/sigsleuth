import type {
  ReplyRoute,
  ReportPaths,
  PacketDetail,
  PacketReception,
  RoutePath,
} from "./api";
import { hopGroup, hopTone, hopsUsed } from "./nodeDisplay";

/**
 * A row of a traceroute's reception table: a gateway's report, or on a request
 * the route its reply says reached the target. alsoReported counts the hidden
 * reports that repeat this one or only its start.
 */
export type TracerouteRow =
  | {
      kind: "reception";
      reception: PacketReception;
      alsoReported: number;
    }
  | { kind: "reply"; reply: ReplyRoute; alsoReported: number };

export interface TracerouteGroup {
  label: string;
  tone: string;
  rows: TracerouteRow[];
}

// Separates a reply's forward route from its return route in a report key.
const RETURN = -1;

/**
 * A traceroute's receptions for its table: only the longest reports, as a
 * report that is the start of another (A→B under A→B→C), or repeats one, says
 * nothing new. Reports that reached their end come first, then by hops used,
 * most first, then hops not reported; the longest report first in each.
 *
 * A report reached its end when the API says so (reached), and on a request
 * the reply's route always did. Reports without a readable route stay.
 */
export function tracerouteGroups(detail: PacketDetail): {
  groups: TracerouteGroup[];
  hidden: number;
} {
  const isReply =
    detail.packet.sender_node_id === detail.traceroute!.target_node_id;
  const candidates: Candidate[] = detail.receptions.map((reception) => ({
    row: { kind: "reception", reception, alsoReported: 0 },
    key: reception.paths === null ? null : reportKey(reception.paths),
    reached: reception.reached,
  }));
  // The reply goes first, so it keeps its row over an equal report.
  if (detail.reply_route !== null) {
    candidates.unshift({
      row: { kind: "reply", reply: detail.reply_route, alsoReported: 0 },
      key: detail.reply_route.path.nodes,
      reached: true,
    });
  }
  // A report is hidden when another starts with it and is longer, or equal
  // and earlier; a report that reached its end only under another that did.
  // Each hidden one counts towards the first kept report that covers it.
  const order = new Map(
    candidates.map((candidate, index) => [candidate, index]),
  );
  const covers = (other: Candidate, candidate: Candidate) =>
    other !== candidate &&
    other.key !== null &&
    candidate.key !== null &&
    (other.reached || !candidate.reached) &&
    startsWith(other.key, candidate.key) &&
    (other.key.length > candidate.key.length ||
      order.get(other)! < order.get(candidate)!);
  const kept = candidates.filter(
    (candidate) => !candidates.some((other) => covers(other, candidate)),
  );
  const keptSet = new Set(kept);
  for (const candidate of candidates) {
    if (keptSet.has(candidate)) continue;
    kept.find((other) => covers(other, candidate))!.row.alsoReported += 1;
  }

  const groups = new Map<string, Group>();
  for (const candidate of kept) {
    const { row, reached } = candidate;
    const reception = row.kind === "reception" ? row.reception : null;
    const label = reached
      ? isReply
        ? "Reached the requester"
        : "Reached the target"
      : hopGroup(reception!);
    let group = groups.get(label);
    if (group === undefined) {
      group = {
        label,
        tone: reached ? "hop-tone-direct" : hopTone(reception!),
        rank: reached ? [0] : hopRank(reception!),
        candidates: [],
      };
      groups.set(label, group);
    }
    group.candidates.push(candidate);
  }
  return {
    groups: [...groups.values()]
      .sort((a, b) => compare(a.rank, b.rank))
      .map(({ label, tone, candidates }) => ({
        label,
        tone,
        rows: candidates
          .sort((a, b) => (b.key?.length ?? 0) - (a.key?.length ?? 0))
          .map((candidate) => candidate.row),
      })),
    hidden: candidates.length - kept.length,
  };
}

/**
 * On a reply, the route to the target its reports share, to show once above
 * them: the target writes it and relays add themselves only to the route back.
 * The most common one is taken, as a report rarely differs. Null on a request
 * or when no report is readable.
 */
export function sharedForwardRoute(detail: PacketDetail): RoutePath | null {
  if (detail.packet.sender_node_id !== detail.traceroute?.target_node_id) {
    return null;
  }
  const counts = new Map<string, { path: RoutePath; count: number }>();
  for (const reception of detail.receptions) {
    if (reception.paths === null) continue;
    const path = reception.paths.forward;
    const key = JSON.stringify(path);
    const entry = counts.get(key) ?? { path, count: 0 };
    entry.count += 1;
    counts.set(key, entry);
  }
  let shared: { path: RoutePath; count: number } | null = null;
  for (const entry of counts.values()) {
    if (shared === null || entry.count > shared.count) shared = entry;
  }
  return shared?.path ?? null;
}

export function samePath(a: RoutePath, b: RoutePath): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

interface Candidate {
  row: TracerouteRow;
  key: (number | null)[] | null;
  reached: boolean;
}

interface Group {
  label: string;
  tone: string;
  rank: number[];
  candidates: Candidate[];
}

// Most hops first, then no hops, then heard directly, then hops not reported,
// then the sender's own upload.
function hopRank(reception: PacketReception): number[] {
  if (reception.own_upload) return [3];
  const hops = hopsUsed(reception);
  if (hops === "?") return [2];
  return [1, -Number(hops), reception.direct ? 1 : 0];
}

function compare(a: number[], b: number[]): number {
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const difference = (a[index] ?? 0) - (b[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

/**
 * The nodes a report's packet had reached: a request's route up to its last
 * relay, or a reply's whole forward route and its return route so far.
 */
function reportKey(paths: ReportPaths): (number | null)[] {
  const reached = paths.forward.nodes.slice(0, paths.forward.snr_db.length + 1);
  if (paths.return_path === null) return reached;
  return [
    ...paths.forward.nodes,
    RETURN,
    ...paths.return_path.nodes.slice(0, paths.return_path.snr_db.length + 1),
  ];
}

function startsWith<T>(whole: T[], start: T[]): boolean {
  return (
    start.length <= whole.length &&
    start.every((node, index) => whole[index] === node)
  );
}

/**
 * The groups split into the rows whose reception is in ids and the rest, each
 * part keeping the groups' order and leaving out the groups it empties.
 */
export function splitGroups(
  groups: TracerouteGroup[],
  ids: ReadonlySet<number>,
): { matching: TracerouteGroup[]; others: TracerouteGroup[] } {
  const part = (keep: boolean) =>
    groups
      .map((group) => ({
        ...group,
        rows: group.rows.filter(
          (row) =>
            ids.has(
              row.kind === "reception"
                ? row.reception.reception_id
                : row.reply.reception_id,
            ) === keep,
        ),
      }))
      .filter((group) => group.rows.length > 0);
  return { matching: part(true), others: part(false) };
}
