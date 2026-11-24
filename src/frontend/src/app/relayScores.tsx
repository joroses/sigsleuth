import { ReactNode, useState } from "react";

import type { RelayCandidate, RelayCheckDetail, RelayCheckReport } from "./api";
import { formatKm } from "./nodeDisplay";
import {
  CheckNode,
  byProbability,
  byteText,
  candidateName,
  nodeText,
  percent,
  radioSettings,
} from "./relayText";
import { Badge, NodeLink, ShortTime, WideTable } from "./ui";

// The parts of relay checks that survey checks share: the candidates, their
// scores, signals, links and the gateways' reports.

type Term = RelayCandidate["terms"][number]["term"];
/** A scored copy: a relay check's, or a survey measurement's. */
export type Scored = Pick<
  RelayCheckDetail,
  | "candidates"
  | "unknown_probability"
  | "rules"
  | "reports"
  | "signals"
  | "links"
  | "one_hop"
  | "spreading_factor"
  | "bandwidth_khz"
>;

// Candidates the score table shows before "Show all".
const SCORE_ROWS = 8;

const TERM_LABELS: Record<Term, string> = {
  unplaced: "no position",
  estimated: "estimated position",
  log_km: "distance",
  beyond_reach: "beyond its reach",
  near5: "under 5 km",
  in_reach: "within reach",
  receiver_far: "receiver far",
  glink: "linked to a co-hearer",
  glink_log: "co-hearer links",
  slink: "linked to the sender",
  slink_log: "sender links",
  sender_far: "sender far",
  snr_llr: "signal match",
  rep_plus1: "reported one hop earlier",
  rep_same: "reported, same hops",
  rep_other: "reported, other hops",
  role_router: "ROUTER",
  role_router_late: "ROUTER_LATE",
  role_client: "CLIENT",
  role_mute: "CLIENT_MUTE",
  role_none: "no role",
  channel_same: "channel on the same radio settings",
  channel_other: "channel on other radio settings",
  inactive: "not active",
  rheard: "heard by the gateway",
  r_rssi: "receiver RSSI match",
  nlink: "linked to a node nearby",
  nlink_log: "links nearby",
  nlink_km: "distance to the nearest linked node",
  nlink_any: "some candidate linked nearby",
  sheard: "heard by a survey nearby",
  survey_only: "known only to surveys",
};

export function RouteBadge() {
  return (
    <>
      {" "}
      <Badge tone="blue" title="The relay this copy's route names">
        route
      </Badge>
    </>
  );
}

export function NodeRef({ node }: { node: CheckNode | null }) {
  if (node === null) return <span className="muted">—</span>;
  if (node.node_id === null)
    return <span className="muted">an opted-out node</span>;
  return <NodeLink nodeId={node.node_id} name={node.name} />;
}

function firmwareText(version: number | null): string {
  if (version === null) return "—";
  return `${Math.floor(version / 10000)}.${Math.floor(version / 100) % 100}.${version % 100}`;
}

const GATEWAY_HISTORY_TERMS = new Set<Term>(["rheard", "r_rssi"]);
const NEARBY_TERMS = new Set<Term>([
  "nlink",
  "nlink_log",
  "nlink_km",
  "nlink_any",
  "sheard",
]);
const COHEARER_TERMS = new Set<Term>([
  "glink",
  "glink_log",
  "snr_llr",
  "rep_plus1",
  "rep_same",
  "rep_other",
]);
const SENDER_TERMS = new Set<Term>(["slink", "slink_log", "sender_far"]);
const DISTANCE_TERMS = new Set<Term>([
  "log_km",
  "near5",
  "unplaced",
  "estimated",
  "in_reach",
  "beyond_reach",
  "receiver_far",
]);
const PROFILE_TERMS = new Set<Term>([
  "role_router",
  "role_router_late",
  "role_client",
  "role_mute",
  "role_none",
  "channel_same",
  "channel_other",
  "inactive",
  "survey_only",
]);

function groupNetScore(candidate: RelayCandidate, terms: Set<Term>): number {
  let sum = 0;
  for (const term of candidate.terms) {
    if (terms.has(term.term)) sum += term.score;
  }
  return sum;
}

function hasTerm(candidate: RelayCandidate, term: Term): boolean {
  return candidate.terms.some((item) => item.term === term);
}

function groupScoreClass(score: number): string {
  if (score > 0.005) return "for";
  if (score < -0.005) return "against";
  return "muted";
}

function formatGroupScore(score: number): string {
  return Math.abs(score) < 0.005 ? "0" : signed(score);
}

/** Unknown probability from which the why table shows the Unknown column:
 * below it, an unknown node barely moves the verdict. */
export const SHOW_UNKNOWN_FROM = 0.05;

export function WhyComparison({
  detail,
  top,
  compare,
  topLabel,
  compareLabel,
  routeKey,
  showGatewayHistory,
  showUnknown,
  senderNodeId = null,
  gatewayNodeId = null,
}: {
  detail: Scored;
  top: RelayCandidate | undefined;
  compare: RelayCandidate | undefined;
  topLabel: string;
  compareLabel?: string;
  routeKey: number | null;
  showGatewayHistory: boolean;
  showUnknown: boolean;
  senderNodeId?: number | null;
  gatewayNodeId?: number | null;
}) {
  if (top === undefined) return null;
  const shown =
    compare !== undefined && compare.key !== top.key
      ? [
          { candidate: top, label: topLabel },
          { candidate: compare, label: compareLabel ?? "Compared" },
        ]
      : [{ candidate: top, label: topLabel }];

  const receiverReport = detail.reports.find((r) => r.kind === "receiver");
  const cohearerReports = detail.reports.filter((r) => r.kind === "cohearer");
  const cohearerIds = new Set(
    cohearerReports
      .map((r) => r.gateway.node_id)
      .filter((id): id is number => id !== null),
  );
  const soundingReports = detail.reports.filter(
    (r) => r.signal && r.snr_db !== null && r.gateway.node_id !== null,
  );

  function gatewayHistoryFact(candidate: RelayCandidate): ReactNode {
    const parts: string[] = [];
    if (candidate.heard_directly > 0) {
      parts.push(`Heard directly ${candidate.heard_directly}×`);
    }
    const signal =
      gatewayNodeId === null
        ? undefined
        : detail.signals.find(
            (s) =>
              s.key === candidate.key && s.gateway_node_id === gatewayNodeId,
          );
    if (
      signal !== undefined &&
      signal.rssi_mean_dbm !== null &&
      receiverReport?.rssi_dbm !== null &&
      receiverReport?.rssi_dbm !== undefined
    ) {
      parts.push(
        `RSSI ${receiverReport.rssi_dbm} vs ${Math.round(signal.rssi_mean_dbm)} dBm`,
      );
    }
    return parts.length > 0 ? parts.join(", ") : "Not heard directly";
  }

  function nearbyFact(candidate: RelayCandidate): ReactNode {
    const surveyed = hasTerm(candidate, "sheard");
    if (candidate.near_partner !== null) {
      return (
        <>
          Linked to <NodeRef node={candidate.near_partner} /> (
          {formatKm(candidate.near_km)} km, {candidate.near_observations} obs)
          {surveyed ? "; heard by a survey nearby" : null}
        </>
      );
    }
    if (surveyed) return "Heard by a survey nearby";
    return hasTerm(candidate, "nlink_any")
      ? "None (others have nearby links)"
      : "None";
  }

  function cohearerFact(candidate: RelayCandidate): ReactNode {
    const parts: string[] = [];
    const linked = detail.links.filter(
      (link) =>
        link.key === candidate.key &&
        cohearerIds.has(link.node_id) &&
        link.radio > 0,
    );
    if (linked.length > 0) {
      parts.push(
        `${linked.length} co-hearer${linked.length === 1 ? "" : "s"} linked`,
      );
    }
    const snrTerm = candidate.terms.find((term) => term.term === "snr_llr");
    if (snrTerm !== undefined) {
      const matched = soundingReports.filter((report) =>
        detail.signals.some(
          (signal) =>
            signal.key === candidate.key &&
            signal.gateway_node_id === report.gateway.node_id &&
            signal.snr_mean_db !== null,
        ),
      );
      if (matched.length > 0) {
        const names = matched
          .slice(0, 2)
          .map((report) => nodeText(report.gateway))
          .join(", ");
        parts.push(
          snrTerm.score >= 0
            ? `SNR matches ${names}`
            : `SNR differs at ${names}`,
        );
      } else {
        parts.push(snrTerm.score >= 0 ? "SNR matches" : "SNR differs");
      }
    }
    if (hasTerm(candidate, "rep_plus1")) parts.push("reported 1 hop earlier");
    else if (hasTerm(candidate, "rep_same")) parts.push("reported same hops");
    else if (hasTerm(candidate, "rep_other")) parts.push("reported other hops");
    return parts.length > 0 ? parts.join("; ") : "None";
  }

  function senderFact(candidate: RelayCandidate): ReactNode {
    const parts: string[] = [];
    const link =
      senderNodeId === null
        ? undefined
        : detail.links.find(
            (item) =>
              item.key === candidate.key && item.node_id === senderNodeId,
          );
    if (link !== undefined && (link.radio > 0 || link.traceroute > 0)) {
      const counts = [
        link.radio > 0 ? `${link.radio} radio` : null,
        link.traceroute > 0 ? `${link.traceroute} route` : null,
      ]
        .filter((item): item is string => item !== null)
        .join(", ");
      parts.push(`Linked (${counts})`);
    }
    if (hasTerm(candidate, "sender_far") && candidate.sender_km !== null) {
      parts.push(`${formatKm(candidate.sender_km)} km away (far)`);
    }
    if (parts.length > 0) return parts.join("; ");
    return candidate.sender_km !== null
      ? `No link (${formatKm(candidate.sender_km)} km)`
      : "No link";
  }

  function distanceFact(candidate: RelayCandidate): ReactNode {
    if (candidate.latitude === null) return "Unplaced";
    if (candidate.receiver_km === null) return "Placed (receiver unplaced)";
    return `${formatKm(candidate.receiver_km)} km away${candidate.estimated ? " (estimated)" : ""}`;
  }

  function profileFact(candidate: RelayCandidate): ReactNode {
    const channel = hasTerm(candidate, "channel_same")
      ? "same channel"
      : hasTerm(candidate, "channel_other")
        ? "other channel"
        : null;
    const activity = hasTerm(candidate, "inactive") ? "inactive" : "active";
    return [candidate.role ?? "no role", channel, activity]
      .filter((item): item is string => item !== null)
      .join(", ");
  }

  const rows: {
    key: string;
    label: string;
    terms: Set<Term>;
    fact: (candidate: RelayCandidate) => ReactNode;
  }[] = [
    ...(showGatewayHistory
      ? [
          {
            key: "gateway",
            label: "Gateway history",
            terms: GATEWAY_HISTORY_TERMS,
            fact: gatewayHistoryFact,
          },
        ]
      : []),
    {
      key: "nearby",
      label: `Nearby nodes (≤ ${detail.rules.near_km} km)`,
      terms: NEARBY_TERMS,
      fact: nearbyFact,
    },
    {
      key: "cohearers",
      label: "Co-hearing gateways",
      terms: COHEARER_TERMS,
      fact: cohearerFact,
    },
    ...(detail.one_hop
      ? [
          {
            key: "sender",
            label: "Sender link",
            terms: SENDER_TERMS,
            fact: senderFact,
          },
        ]
      : []),
    {
      key: "distance",
      label: "Distance & position",
      terms: DISTANCE_TERMS,
      fact: distanceFact,
    },
    {
      key: "profile",
      label: "Node profile",
      terms: PROFILE_TERMS,
      fact: profileFact,
    },
  ];

  return (
    <WideTable>
      <table className="relay-why-table">
        <thead>
          <tr>
            <th>Evidence</th>
            {shown.map(({ candidate, label }) => (
              <th key={candidate.key}>
                <span className="relay-why-head">
                  {candidate.node_id === null ? (
                    <span className="muted">an opted-out node</span>
                  ) : (
                    <NodeLink
                      nodeId={candidate.node_id}
                      name={candidateName(candidate)}
                    />
                  )}
                  {candidate.key === routeKey ? <RouteBadge /> : null}
                </span>
                <span className="column-note">
                  {label} · {percent(candidate.probability)} (score{" "}
                  {signed(candidate.score ?? 0)})
                </span>
              </th>
            ))}
            {showUnknown ? (
              <th>
                Unknown node
                <span className="column-note">
                  {percent(detail.unknown_probability)} (score{" "}
                  {signed(detail.rules.unknown_score)})
                </span>
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <th scope="row">{row.label}</th>
              {shown.map(({ candidate }) => {
                const net = groupNetScore(candidate, row.terms);
                return (
                  <td key={candidate.key}>
                    <span className="relay-why-fact">
                      {row.fact(candidate)}
                    </span>
                    <span
                      className={`relay-why-points ${groupScoreClass(net)}`}
                    >
                      {formatGroupScore(net)}
                    </span>
                  </td>
                );
              })}
              {showUnknown ? <td className="muted">—</td> : null}
            </tr>
          ))}
        </tbody>
      </table>
    </WideTable>
  );
}

export function CandidateTable({
  detail,
  routeKey,
  selected,
  onSelect,
  ruledOut = null,
  heardColumn = true,
}: {
  detail: Scored;
  routeKey: number | null;
  selected: number | null;
  onSelect: (key: number) => void;
  /** Candidates a survey's signal rule left out, and its dB. */
  ruledOut?: { keys: number[]; db: number } | null;
  /** Shows how often the receiver heard each directly. */
  heardColumn?: boolean;
}) {
  const [allShown, setAllShown] = useState(false);
  const rules = detail.rules;
  const sorted = byProbability(detail.candidates);
  const hidden = (candidate: RelayCandidate) =>
    candidate.key !== routeKey &&
    !candidate.possible &&
    !ruledOut?.keys.includes(candidate.key);
  const hiddenCount = sorted.filter(hidden).length;
  const rows = allShown ? sorted : sorted.filter((c) => !hidden(c));
  return (
    <>
      <WideTable>
        <table className="relay-candidate-table">
          <thead>
            <tr>
              <th>Candidate</th>
              <th className="number">Probability</th>
              <th>Position</th>
              <th>Role</th>
              <th>
                Radio settings
                <span className="column-note">
                  copy{" "}
                  {detail.spreading_factor === null
                    ? "unknown"
                    : radioSettings(
                        detail.spreading_factor,
                        detail.bandwidth_khz,
                      )}
                </span>
              </th>
              <th>
                Firmware
                <span className="column-note">relay byte from 2.6</span>
              </th>
              <th>
                Receiver km
                <span className="column-note">of its reach</span>
              </th>
              <th>
                Linked nearby
                <span className="column-note">observations</span>
              </th>
              <th>
                Own report
                <span className="column-note">hops left</span>
              </th>
              <th>
                Active
                <span className="column-note">
                  within {rules.active_hours} h
                </span>
              </th>
              <th>
                Sender km
                <span className="column-note">one hop only</span>
              </th>
              {heardColumn ? <th className="number">Heard directly</th> : null}
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <tr
                key={candidate.key}
                className={[
                  "selectable-row",
                  candidate.key === routeKey ? "route-relay-row" : "",
                  candidate.key === selected ? "selected-row" : "",
                ].join(" ")}
                onClick={() => onSelect(candidate.key)}
              >
                <th scope="row">
                  {candidate.node_id === null ? (
                    <span className="muted">an opted-out node</span>
                  ) : (
                    <NodeLink
                      nodeId={candidate.node_id}
                      name={candidateName(candidate)}
                    />
                  )}
                  {candidate.key === routeKey ? <RouteBadge /> : null}
                </th>
                <td className="number">
                  {candidate.possible ? (
                    percent(candidate.probability)
                  ) : ruledOut?.keys.includes(candidate.key) ? (
                    <Badge tone="amber" title="Too strong to be its copy">
                      {ruledOut.db} dB
                    </Badge>
                  ) : (
                    <Badge tone="neutral">cannot</Badge>
                  )}
                </td>
                <td>
                  {candidate.latitude === null && candidate.node_id !== null
                    ? "none"
                    : candidate.estimated
                      ? "estimated"
                      : "now"}
                </td>
                <td>{candidate.role ?? "—"}</td>
                <td>
                  {candidate.channel_spreading_factor === null &&
                  candidate.preset_spreading_factor === null
                    ? "unknown"
                    : [
                        candidate.channel_spreading_factor === null
                          ? null
                          : `channel ${radioSettings(
                              candidate.channel_spreading_factor,
                              candidate.channel_bandwidth_khz,
                            )}`,
                        candidate.preset_spreading_factor === null
                          ? null
                          : `map report ${radioSettings(
                              candidate.preset_spreading_factor,
                              candidate.preset_bandwidth_khz,
                            )}`,
                      ]
                        .filter((text) => text !== null)
                        .join(", ")}
                </td>
                <td>{firmwareText(candidate.firmware)}</td>
                <td>
                  <KmOf
                    km={candidate.receiver_km}
                    limit={Math.max(candidate.reach_km, rules.name_km)}
                  />
                </td>
                <td>
                  <NearText candidate={candidate} />
                </td>
                <td>
                  {candidate.own_hop_limit === null ? (
                    <span className="muted">no report</span>
                  ) : (
                    `${candidate.own_hop_limit} left`
                  )}
                </td>
                <td>
                  <ActivityText candidate={candidate} />
                </td>
                <td>
                  {!detail.one_hop ? (
                    <span className="muted">—</span>
                  ) : (
                    <KmOf
                      km={candidate.sender_km}
                      limit={Math.max(candidate.reach_km, rules.far_km)}
                    />
                  )}
                </td>
                {heardColumn ? (
                  <td className="number">
                    {candidate.heard_directly > 0
                      ? candidate.heard_directly
                      : "—"}
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      </WideTable>
      {hiddenCount > 0 ? (
        <button
          type="button"
          className="text-button relay-more"
          onClick={() => setAllShown(!allShown)}
        >
          {allShown
            ? "Hide those that cannot have sent it"
            : `Show ${hiddenCount} more that cannot have sent it`}
        </button>
      ) : null}
    </>
  );
}

/** km and the limit it is held to, with a decimal when rounding would hide which is larger. */
function KmOf({ km, limit }: { km: number | null; limit: number }) {
  const close = km !== null && formatKm(km) === formatKm(limit);
  return (
    <>
      {close ? km.toFixed(1) : formatKm(km)}
      <span className="muted"> of {formatKm(limit)}</span>
    </>
  );
}

/** The most observed node near the receiver linked with it, and all such links. */
function NearText({ candidate }: { candidate: RelayCandidate }) {
  if (candidate.near_partner === null) return <span className="muted">—</span>;
  return (
    <>
      <NodeRef node={candidate.near_partner} /> {formatKm(candidate.near_km)} km
      <span className="muted"> · {candidate.near_observations}</span>
    </>
  );
}

function ActivityText({ candidate }: { candidate: RelayCandidate }) {
  const hours = candidate.activity_hours;
  const gateway =
    candidate.last_gateway_at === null ? null : (
      <span className="muted">
        {" "}
        · gateway <ShortTime value={candidate.last_gateway_at} />
      </span>
    );
  return (
    <>
      {hours === null
        ? "none near"
        : hours === 0
          ? "same hour"
          : `${hours > 0 ? "+" : "−"}${Math.abs(hours)} h`}
      {gateway}
    </>
  );
}

function signed(value: number): string {
  return `${value < 0 ? "−" : "+"}${Math.abs(value).toFixed(2)}`;
}

export function ScoreTable({
  detail,
  routeKey,
}: {
  detail: Scored;
  routeKey: number | null;
}) {
  const [allShown, setAllShown] = useState(false);
  const possible = byProbability(
    detail.candidates.filter((candidate) => candidate.possible),
  );
  const rows = allShown ? possible : possible.slice(0, SCORE_ROWS);
  if (possible.length === 0) return null;
  return (
    <>
      <WideTable>
        <table className="relay-step-table">
          <thead>
            <tr>
              <th>Candidate</th>
              <th className="number">Probability</th>
              <th className="number">Score</th>
              <th>Terms, largest first</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <tr key={candidate.key}>
                <th scope="row">
                  {candidateName(candidate)}
                  {candidate.key === routeKey ? <RouteBadge /> : null}
                  {candidate.evidence ? null : (
                    <span className="muted"> · no evidence</span>
                  )}
                </th>
                <td className="number">{percent(candidate.probability)}</td>
                <td className="number">{signed(candidate.score ?? 0)}</td>
                <td className="relay-terms">
                  {candidate.terms.map((term) => (
                    <span
                      key={term.term}
                      className={term.score < 0 ? "against" : "for"}
                      title={`Value ${term.value}`}
                    >
                      {TERM_LABELS[term.term]} {signed(term.score)}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
            <tr className="muted">
              <th scope="row">A node SigSleuth does not know</th>
              <td className="number">{percent(detail.unknown_probability)}</td>
              <td className="number">{signed(detail.rules.unknown_score)}</td>
              <td />
            </tr>
          </tbody>
        </table>
      </WideTable>
      {possible.length > SCORE_ROWS ? (
        <button
          type="button"
          className="text-button relay-more"
          onClick={() => setAllShown(!allShown)}
        >
          {allShown
            ? `Show the ${SCORE_ROWS} likeliest`
            : `Show all ${possible.length}`}
        </button>
      ) : null}
    </>
  );
}

/** The co-hearers' SNR against each candidate's usual direct SNR there. */
export function SignalTable({
  detail,
  routeKey,
  includeReceiver = true,
}: {
  detail: Scored;
  routeKey: number | null;
  includeReceiver?: boolean;
}) {
  const copies = detail.reports.filter(
    (report) =>
      report.signal || (includeReceiver && report.kind === "receiver"),
  );
  if (copies.length === 0 || detail.signals.length === 0) return null;
  const profile = new Map(
    detail.signals.map((signal) => [
      `${signal.key}/${signal.gateway_node_id}`,
      signal,
    ]),
  );
  const keys = new Set(detail.signals.map((signal) => signal.key));
  const rows = byProbability(
    detail.candidates.filter((candidate) => keys.has(candidate.key)),
  );
  return (
    <>
      <h4 className="relay-score-figures">Direct signal profiles</h4>
      <WideTable>
        <table className="relay-step-table">
          <thead>
            <tr>
              <th>Candidate</th>
              {copies.map((report) => (
                <th key={report.reception_id}>
                  {nodeText(report.gateway)}
                  <span className="column-note">
                    copy {report.snr_db ?? "—"} dB · {report.rssi_dbm ?? "—"}{" "}
                    dBm
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <tr key={candidate.key}>
                <th scope="row">
                  {candidateName(candidate)}
                  {candidate.key === routeKey ? <RouteBadge /> : null}
                </th>
                {copies.map((report) => {
                  const signal = profile.get(
                    `${candidate.key}/${report.gateway.node_id}`,
                  );
                  return (
                    <td key={report.reception_id}>
                      {signal === undefined ? (
                        <span className="muted">never heard</span>
                      ) : (
                        <>
                          {signal.snr_mean_db?.toFixed(1) ?? "—"} dB
                          <span className="muted"> ({signal.snr_count})</span>
                          <br />
                          {signal.rssi_mean_dbm?.toFixed(1) ?? "—"} dBm
                          <span className="muted"> ({signal.rssi_count})</span>
                        </>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </WideTable>
    </>
  );
}

/** Radio links with the co-hearers, and radio links and traceroute hops with the sender after one hop. */
export function LinkTable({
  detail,
  sender: senderNode,
  routeKey,
}: {
  detail: Scored;
  sender: CheckNode;
  routeKey: number | null;
}) {
  if (detail.links.length === 0) return null;
  const sender = senderNode.node_id;
  const others = [
    ...detail.reports
      .filter((report) => report.kind === "cohearer")
      .map((report) => report.gateway),
    ...(detail.one_hop ? [senderNode] : []),
  ];
  const links = new Map(
    detail.links.map((link) => [`${link.key}/${link.node_id}`, link]),
  );
  const keys = new Set(detail.links.map((link) => link.key));
  const rows = byProbability(
    detail.candidates.filter((candidate) => keys.has(candidate.key)),
  );
  return (
    <>
      <h4 className="relay-score-figures">Links</h4>
      <WideTable>
        <table className="relay-step-table">
          <thead>
            <tr>
              <th>Candidate</th>
              {others.map((other) => (
                <th key={other.node_id}>
                  {nodeText(other)}
                  {other.node_id === sender ? (
                    <span className="column-note">
                      sender: radio + traceroute
                    </span>
                  ) : null}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <tr key={candidate.key}>
                <th scope="row">
                  {candidateName(candidate)}
                  {candidate.key === routeKey ? <RouteBadge /> : null}
                </th>
                {others.map((other) => {
                  const link = links.get(`${candidate.key}/${other.node_id}`);
                  return (
                    <td key={other.node_id}>
                      {link === undefined
                        ? "—"
                        : other.node_id === sender
                          ? `${link.radio} + ${link.traceroute}`
                          : link.radio}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </WideTable>
    </>
  );
}

export function ReportTable({
  reports: given,
  copyGateway = null,
  routes = true,
}: {
  reports: RelayCheckReport[];
  /** A survey receiver's stand-in: the gateway that reported this copy. */
  copyGateway?: number | null;
  /** Shows each report's route relay (traceroutes). */
  routes?: boolean;
}) {
  const KIND_ORDER = ["receiver", "cohearer", "earlier", "other", "sender"];
  const KIND_LABELS: Record<RelayCheckReport["kind"], string> = {
    receiver: "this copy",
    cohearer: "co-hearer",
    earlier: "one more hop left",
    other: "other",
    sender: "sender's own upload",
  };
  const reports = [...given].sort(
    (a, b) =>
      KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) ||
      (b.hop_limit ?? -1) - (a.hop_limit ?? -1),
  );
  return (
    <WideTable>
      <table className="relay-report-table">
        <thead>
          <tr>
            <th>Gateway</th>
            <th>Kind</th>
            <th className="number">Hops left</th>
            <th>Relay byte</th>
            <th className="number">SNR</th>
            <th className="number">RSSI</th>
            {routes ? <th>Its route&apos;s relay</th> : null}
          </tr>
        </thead>
        <tbody>
          {reports.map((report) => {
            return (
              <tr
                key={report.reception_id}
                className={
                  report.kind === "receiver" ? "route-relay-row" : undefined
                }
              >
                <th scope="row">
                  <NodeRef node={report.gateway} />
                  {report.sends_byte ? (
                    <span className="muted"> · sends the byte</span>
                  ) : null}
                </th>
                <td>
                  {KIND_LABELS[report.kind]}
                  {report.signal ? (
                    <span className="muted"> · used for signal match</span>
                  ) : null}
                  {report.route_conflict ? (
                    <span className="muted"> · different route</span>
                  ) : null}
                  {copyGateway !== null &&
                  report.gateway.node_id === copyGateway ? (
                    <span className="muted">
                      {" "}
                      · excluded (standing in as phone)
                    </span>
                  ) : null}
                  {report.direct ? (
                    <span className="muted"> · heard the sender</span>
                  ) : null}
                </td>
                <td className="number">{report.hop_limit ?? "—"}</td>
                <td>
                  {report.relay_byte === null || report.relay_byte === 0
                    ? "—"
                    : byteText(report.relay_byte)}
                </td>
                <td className="number">
                  {report.snr_db === null ? "—" : `${report.snr_db} dB`}
                </td>
                <td className="number">
                  {report.rssi_dbm === null ? "—" : `${report.rssi_dbm} dBm`}
                </td>
                {routes ? (
                  <td
                    className={report.route_conflict ? "rule-hit" : undefined}
                  >
                    {report.route_relay !== null ? (
                      <NodeRef node={report.route_relay} />
                    ) : report.route_hint !== null ? (
                      <>
                        <span className="muted">possibly </span>
                        <NodeRef node={report.route_hint} />
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </WideTable>
  );
}
