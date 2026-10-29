import { useState } from "react";

import type { RelayCandidate, RelayCheckDetail, RelayCheckReport } from "./api";
import { formatKm } from "./nodeDisplay";
import {
  CheckNode,
  byteText,
  candidateName,
  nodeText,
  percent,
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
  channel_same: "channel on this SF",
  channel_other: "channel on another SF",
  inactive: "not active",
  rheard: "heard by the gateway",
  r_rssi: "receiver RSSI match",
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

/** Candidates by probability, those that cannot have sent the copy last. */
function byProbability(candidates: RelayCandidate[]): RelayCandidate[] {
  return [...candidates].sort(
    (a, b) =>
      (b.probability ?? -1) - (a.probability ?? -1) ||
      (a.receiver_km ?? Infinity) - (b.receiver_km ?? Infinity),
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
  const copySf = detail.spreading_factor;
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
                Spreading factor
                <span className="column-note">
                  copy {copySf === null ? "unknown" : `SF${copySf}`}
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
                          : `channel SF${candidate.channel_spreading_factor}`,
                        candidate.preset_spreading_factor === null
                          ? null
                          : `map report SF${candidate.preset_spreading_factor}`,
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
}: {
  detail: Scored;
  routeKey: number | null;
}) {
  const copies = detail.reports.filter(
    (report) => report.signal || report.kind === "receiver",
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
                    <span className="muted"> · signal term</span>
                  ) : null}
                  {report.route_conflict ? (
                    <span className="muted"> · different route</span>
                  ) : null}
                  {copyGateway !== null &&
                  report.gateway.node_id === copyGateway ? (
                    <span className="muted"> · this copy&apos;s gateway</span>
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
