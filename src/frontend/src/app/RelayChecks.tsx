import { ReactNode, useEffect, useMemo, useState } from "react";

import {
  RelayCandidate,
  RelayCheck,
  RelayCheckDetail,
  RelayCheckList,
  RelayCheckReport,
  RelayCheckStep,
  relayCheckUrl,
  relayChecksUrl,
} from "./api";
import {
  PLACED_BY,
  formatKm,
  formatNodeId,
  relayChecksHref,
} from "./nodeDisplay";
import { RelayCheckMap } from "./RelayCheckMap";
import { useSetting, useViewState } from "./remember";
import { useApi, usePagedApi } from "./useApi";
import { Badge, NodeLink, ShortTime, StatusPanel, WideTable } from "./ui";

type Answer = "rules" | "page";
type Outcome = NonNullable<RelayCheck["rules_outcome"]>;
type Show = Outcome | "not_right" | "all";
type StepName = NonNullable<RelayCheck["rules_step"]>;
type RuleOut = RelayCandidate["ruled_out"][number];
type CheckNode = RelayCheck["sender"];

const ANSWERS: { value: Answer; label: string }[] = [
  { value: "rules", label: "Rules only (as for a survey)" },
  { value: "page", label: "Packet page (heard directly first)" },
];
const SHOWS: { value: Show; label: string }[] = [
  { value: "not_right", label: "Wrong and undecided" },
  { value: "wrong", label: "Wrong" },
  { value: "undecided", label: "Undecided" },
  { value: "right", label: "Right" },
  { value: "all", label: "All" },
];
const COUNTS = [25, 50, 100, 200];

const STEP_LABELS: Record<StepName, string> = {
  heard_directly: "Heard directly",
  signal: "Signal match",
  gateway_link: "Links with co-hearers",
  sender_link: "Links with the sender",
  reported: "Reported one hop earlier",
  only_candidate: "Only candidate",
};

const RULE_OUT_LABELS: Record<RuleOut, string> = {
  unplaced: "no position",
  client_mute: "CLIENT_MUTE",
  channel: "channel on another SF",
  preset: "map report on another SF",
  firmware: "firmware without relay byte",
  receiver_far: "receiver beyond its reach",
  own_report: "its own report's hops",
  silent: "not active within 24 h",
  sender_far: "sender beyond its reach",
};

const OUTCOME_TONES = {
  right: "green",
  wrong: "red",
  undecided: "amber",
} as const;

/**
 * Diagnostics' relay checks (#diagnostics/relays): traceroute copies whose
 * route names their relay, against the relay SigSleuth names, and one
 * copy's naming step by step (#diagnostics/relays/<reception>).
 */
export function RelayChecks({ receptionId }: { receptionId: number | null }) {
  // The list loads once it is first shown, and stays loaded while a copy is
  // open, so going back to it is instant.
  const [listShown, setListShown] = useState(receptionId === null);
  if (receptionId === null && !listShown) setListShown(true);
  const [generation, setGeneration] = useState(0);
  const [queue, setQueue] = useState<number[]>([]);
  return (
    <div className="diagnostics-page relay-checks-page">
      {listShown ? (
        <div hidden={receptionId !== null}>
          <CheckList
            key={generation}
            onQueue={setQueue}
            onRefresh={() => setGeneration((value) => value + 1)}
          />
        </div>
      ) : null}
      {receptionId !== null ? (
        <CheckDetail
          key={receptionId}
          receptionId={receptionId}
          queue={queue}
        />
      ) : null}
    </div>
  );
}

function outcomeOf(check: RelayCheck, answer: Answer): Outcome | null {
  return answer === "page" ? check.page_outcome : check.rules_outcome;
}

/** Why a check came out as it did, to group the list by. */
function whyOf(check: RelayCheck, answer: Answer): string {
  const outcome = outcomeOf(check, answer);
  const step = answer === "page" ? check.page_step : check.rules_step;
  const out = check.route_relay_ruled_out;
  const route = check.route_relay?.node_id ?? null;
  if (outcome === "right" && step !== null) {
    return `Named by ${STEP_LABELS[step].toLowerCase()}`;
  }
  if (out === null) return "Route's relay is no candidate";
  if (out.length > 0) {
    return `Route's relay ruled out: ${out.map((rule) => RULE_OUT_LABELS[rule]).join(", ")}`;
  }
  // The steps that point to the route's relay without naming it.
  const missed = check.steps
    .filter((item) => item.pick?.node_id === route && route !== null)
    .map((item) => STEP_LABELS[item.rule].toLowerCase());
  const missedText =
    missed.length === 0 ? "" : `; ${missed.join(", ")} points to it`;
  if (outcome === "wrong" && step !== null) {
    return `Another named by ${STEP_LABELS[step].toLowerCase()}${missedText}`;
  }
  const beyond = check.steps.find((item) => item.pick !== null && !item.named);
  if (beyond !== undefined) {
    const whom = beyond.pick?.node_id === route ? "route's relay" : "another";
    return `${STEP_LABELS[beyond.rule]} points to ${whom}, beyond its naming reach`;
  }
  if (check.steps.some((item) => item.rule === "signal" && item.evidence)) {
    return "Signal match too close to call";
  }
  if (!check.steps.some((item) => item.evidence)) {
    return "No co-hearer, link or report to go on";
  }
  return "Reported one hop earlier: none or several within reach";
}

function shows(outcome: Outcome | null, show: Show): boolean {
  if (show === "all") return true;
  if (show === "not_right") return outcome !== "right";
  return outcome === show;
}

function CheckList({
  onQueue,
  onRefresh,
}: {
  /** The checks listed, in order, for the open copy's previous and next. */
  onQueue: (ids: number[]) => void;
  onRefresh: () => void;
}) {
  const [answer, setAnswer] = useSetting<Answer>(
    "relayChecks.answer",
    "rules",
    (value) => ANSWERS.some((item) => item.value === value),
  );
  const [show, setShow] = useSetting<Show>(
    "relayChecks.show",
    "not_right",
    (value) => SHOWS.some((item) => item.value === value),
  );
  const [count, setCount] = useSetting<number>(
    "relayChecks.count",
    COUNTS[0],
    (value) => COUNTS.includes(value as number),
  );
  const [why, setWhy] = useViewState<string | null>("relayChecks.why", null);
  const state = usePagedApi<RelayCheckList>(
    relayChecksUrl(count),
    (loaded, next) => ({
      ...next,
      checks: [...loaded.checks, ...next.checks],
      traceroutes: loaded.traceroutes + next.traceroutes,
      newest_at: loaded.newest_at,
      oldest_at: next.oldest_at ?? loaded.oldest_at,
    }),
  );
  const data = state.data;
  const checks = useMemo(() => data?.checks ?? [], [data]);
  const counts = { right: 0, wrong: 0, undecided: 0 };
  for (const check of checks) {
    const outcome = outcomeOf(check, answer);
    if (outcome !== null) counts[outcome] += 1;
  }
  const kept = checks.filter((check) => shows(outcomeOf(check, answer), show));
  const whys = new Map<string, number>();
  for (const check of kept) {
    const reason = whyOf(check, answer);
    whys.set(reason, (whys.get(reason) ?? 0) + 1);
  }
  const listed =
    why === null || !whys.has(why)
      ? kept
      : kept.filter((check) => whyOf(check, answer) === why);
  const queueKey = listed.map((check) => check.reception_id).join(",");
  useEffect(() => {
    onQueue(queueKey === "" ? [] : queueKey.split(",").map(Number));
  }, [queueKey, onQueue]);

  return (
    <>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">
            <a className="text-button" href="#diagnostics">
              Diagnostics
            </a>
          </p>
          <h2>Relay checks</h2>
          <p className="diagnostic-lede">
            Traceroute copies whose own route names their relay: the node before
            the gateway, when it sends the copy&apos;s relay byte. Each is
            checked against the relay SigSleuth names. Open one to see what
            ruled each candidate out and which step decided.
          </p>
        </div>
      </div>
      <div className="nodes-toolbar">
        <select
          aria-label="Answer"
          value={answer}
          onChange={(event) => setAnswer(event.target.value as Answer)}
        >
          {ANSWERS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Show"
          value={show}
          onChange={(event) => {
            setShow(event.target.value as Show);
            setWhy(null);
          }}
        >
          {SHOWS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Traceroutes per load"
          value={count}
          onChange={(event) => setCount(Number(event.target.value))}
        >
          {COUNTS.map((value) => (
            <option key={value} value={value}>
              {value} traceroutes
            </option>
          ))}
        </select>
        <button type="button" className="secondary-button" onClick={onRefresh}>
          Refresh
        </button>
      </div>
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : data === null ? (
        <StatusPanel>Checking the newest {count} traceroutes…</StatusPanel>
      ) : (
        <>
          <p className="relay-check-counts">
            {(["right", "wrong", "undecided"] as const).map((outcome) => (
              <Badge key={outcome} tone={OUTCOME_TONES[outcome]}>
                {counts[outcome]} {outcome}
              </Badge>
            ))}
            <span className="muted">
              {checks.length} copies in {data.traceroutes} traceroutes
              {data.oldest_at !== null && data.newest_at !== null ? (
                <>
                  , <ShortTime value={data.oldest_at} /> to{" "}
                  <ShortTime value={data.newest_at} />
                </>
              ) : null}
            </span>
          </p>
          {whys.size > 0 ? (
            <div className="relay-check-whys" role="group" aria-label="Why">
              {[...whys.entries()]
                .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
                .map(([reason, total]) => (
                  <button
                    type="button"
                    key={reason}
                    className="relay-why"
                    aria-pressed={why === reason}
                    onClick={() => setWhy(why === reason ? null : reason)}
                  >
                    {reason} <strong>{total}</strong>
                  </button>
                ))}
            </div>
          ) : null}
          {listed.length > 0 ? (
            <WideTable>
              <table className="relay-check-table">
                <thead>
                  <tr>
                    <th>Heard</th>
                    <th>Result</th>
                    <th>Why</th>
                    <th>Route says</th>
                    <th>Named</th>
                    <th>Sender → gateway</th>
                    <th>Byte · hops</th>
                    <th className="number">Left</th>
                  </tr>
                </thead>
                <tbody>
                  {listed.map((check) => (
                    <CheckRow
                      key={check.reception_id}
                      check={check}
                      answer={answer}
                    />
                  ))}
                </tbody>
              </table>
            </WideTable>
          ) : (
            <StatusPanel>None of these copies are shown.</StatusPanel>
          )}
          {state.loadMoreError !== null ? (
            <StatusPanel tone="error">{state.loadMoreError}</StatusPanel>
          ) : null}
          {data.next_before_id !== null ? (
            <button
              type="button"
              className="secondary-button page-button"
              disabled={state.loadingMore}
              onClick={state.loadMore}
            >
              {state.loadingMore
                ? "Checking…"
                : `Check ${count} older traceroutes`}
            </button>
          ) : null}
        </>
      )}
    </>
  );
}

function CheckRow({ check, answer }: { check: RelayCheck; answer: Answer }) {
  const named = answer === "page" ? check.page_relay : check.rules_relay;
  const step = answer === "page" ? check.page_step : check.rules_step;
  const outcome = outcomeOf(check, answer);
  return (
    <tr>
      <td>
        <a className="text-button" href={relayChecksHref(check.reception_id)}>
          <ShortTime value={check.received_at} />
        </a>
      </td>
      <td>
        {outcome !== null ? (
          <Badge tone={OUTCOME_TONES[outcome]}>{outcome}</Badge>
        ) : null}
        {check.mixed ? <MixedBadge /> : null}
      </td>
      <td>{whyOf(check, answer)}</td>
      <td>
        {check.route_relay === null ? "—" : nodeText(check.route_relay)}
        {check.route_snr_db !== null ? (
          <span className="muted"> {check.route_snr_db} dB</span>
        ) : null}
      </td>
      <td>
        {named === null ? (
          <span className="muted">none</span>
        ) : (
          <>
            {nodeText(named)}
            {step !== null ? (
              <span className="muted"> · {STEP_LABELS[step]}</span>
            ) : null}
          </>
        )}
      </td>
      <td>
        {nodeText(check.sender)} → {nodeText(check.gateway)}
      </td>
      <td>
        {byteText(check.relay_byte)} ·{" "}
        {hopsText(check.hop_start, check.hop_limit)}
      </td>
      <td className="number">
        {check.left} of {check.candidates}
      </td>
    </tr>
  );
}

function RouteBadge() {
  return (
    <>
      {" "}
      <Badge tone="blue" title="The relay this copy's route names">
        route
      </Badge>
    </>
  );
}

function MixedBadge() {
  return (
    <Badge tone="amber" title="Another co-hearer's route names another relay">
      mixed
    </Badge>
  );
}

function nodeText(node: CheckNode | null): string {
  if (node === null) return "—";
  if (node.node_id === null) return "an opted-out node";
  return node.name ?? formatNodeId(node.node_id);
}

function NodeRef({ node }: { node: CheckNode | null }) {
  if (node === null) return <span className="muted">—</span>;
  if (node.node_id === null)
    return <span className="muted">an opted-out node</span>;
  return <NodeLink nodeId={node.node_id} name={node.name} />;
}

function byteText(byte: number): string {
  return byte.toString(16).padStart(2, "0");
}

function hopsText(start: number | null, left: number | null): string {
  if (start === null || left === null) return "hops unknown";
  const used = start - left;
  return `${used} hop${used === 1 ? "" : "s"}`;
}

function firmwareText(version: number | null): string {
  if (version === null) return "—";
  return `${Math.floor(version / 10000)}.${Math.floor(version / 100) % 100}.${version % 100}`;
}

function CheckDetail({
  receptionId,
  queue,
}: {
  receptionId: number;
  queue: number[];
}) {
  const [receiverMode, setReceiverMode] = useViewState<"survey" | "gateway">(
    "relayChecks.receiverMode",
    "survey",
  );
  const [receiverAt, setReceiverAt] = useViewState<[number, number] | null>(
    "relayChecks.receiverAt",
    null,
  );
  const state = useApi<RelayCheckDetail>(
    relayCheckUrl(receptionId, receiverAt, receiverMode),
  );
  // The last answer stays shown while a moved receiver's loads, so the map
  // keeps its view.
  const [shown, setShown] = useState<RelayCheckDetail | null>(null);
  if (state.data !== null && state.data !== shown) setShown(state.data);
  const index = queue.indexOf(receptionId);
  const previous = index > 0 ? queue[index - 1] : null;
  const next = index >= 0 && index < queue.length - 1 ? queue[index + 1] : null;
  return (
    <>
      <nav className="relay-check-nav" aria-label="Relay checks">
        <a className="text-button" href={relayChecksHref()}>
          ← Relay checks
        </a>
        {index >= 0 ? (
          <span className="relay-check-step-through">
            {previous !== null ? (
              <a className="secondary-button" href={relayChecksHref(previous)}>
                Previous
              </a>
            ) : null}
            <span className="muted">
              {index + 1} of {queue.length}
            </span>
            {next !== null ? (
              <a className="secondary-button" href={relayChecksHref(next)}>
                Next
              </a>
            ) : null}
          </span>
        ) : null}
      </nav>
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : shown === null ? (
        <StatusPanel>Loading…</StatusPanel>
      ) : (
        <CheckSteps
          detail={shown}
          updating={state.loading}
          onReceiver={(at) => {
            setReceiverMode("survey");
            setReceiverAt(at);
          }}
          onMode={(mode) => {
            setReceiverAt(null);
            setReceiverMode(mode);
          }}
        />
      )}
    </>
  );
}

function CheckSteps({
  detail,
  updating,
  onReceiver,
  onMode,
}: {
  detail: RelayCheckDetail;
  updating: boolean;
  /** Moves the survey receiver; null resets its position. */
  onReceiver: (at: [number, number] | null) => void;
  onMode: (mode: "survey" | "gateway") => void;
}) {
  const [selected, setSelected] = useState<number | null>(null);
  const check = detail.check;
  const candidates = detail.candidates;
  const byKey = new Map(
    candidates.map((candidate) => [candidate.key, candidate]),
  );
  const left = candidates.filter(
    (candidate) => candidate.ruled_out.length === 0,
  );
  const heard = candidates.filter((candidate) => candidate.heard_directly > 0);
  const decidingIndex = detail.steps.findIndex((step) => step.named !== null);
  const routeKey = check.route_relay?.node_id ?? null;
  const name = (key: number | null) =>
    key === null ? "—" : candidateName(byKey.get(key));
  return (
    <>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">Relay check</p>
          <h2>
            {nodeText(check.sender)} → {nodeText(check.gateway)}
          </h2>
          <p className="diagnostic-lede">
            <ShortTime value={check.received_at} /> ·{" "}
            <a
              className="text-button"
              href={`#packets/${check.packet_row_id}?reception=${check.reception_id}`}
            >
              packet
            </a>{" "}
            · {detail.channel}
            {detail.spreading_factor !== null
              ? ` (SF${detail.spreading_factor})`
              : ""}{" "}
            · relay byte {byteText(check.relay_byte)} ·{" "}
            {check.hop_start === null || check.hop_limit === null
              ? "hops unknown"
              : `${check.hop_limit} of ${check.hop_start} hops left (${hopsText(check.hop_start, check.hop_limit)} used)`}
          </p>
        </div>
      </div>

      <section className="relay-check-verdict" aria-label="Result">
        <dl>
          <div>
            <dt>Route says</dt>
            <dd>
              <NodeRef node={check.route_relay} />
              {check.route_snr_db !== null ? (
                <span className="muted"> heard at {check.route_snr_db} dB</span>
              ) : null}
              <RouteChain detail={detail} />
            </dd>
          </div>
          <div>
            <dt>Packet page names</dt>
            <dd>
              <Answered
                node={check.page_relay}
                step={check.page_step}
                outcome={check.page_outcome}
              />
            </dd>
          </div>
          <div>
            <dt>Rules name</dt>
            <dd>
              <Answered
                node={check.rules_relay}
                step={check.rules_step}
                outcome={check.rules_outcome}
              />
            </dd>
          </div>
        </dl>
        {check.mixed ? (
          <p className="relay-check-warning">
            <MixedBadge /> Another gateway heard a copy with the same byte and
            hops left, but its route names another relay: two nodes sending this
            byte rebroadcast it, so the co-hearers&apos; signals and links mix
            two copies.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="relay-map-heading">
        <div className="section-intro compact">
          <h3 id="relay-map-heading">Where they are</h3>
          <PlacementNotes detail={detail} />
        </div>
        <select
          aria-label="Receiver"
          value={detail.receiver_mode}
          onChange={(event) =>
            onMode(event.target.value as "survey" | "gateway")
          }
        >
          <option value="survey">Survey receiver</option>
          <option value="gateway">Reporting gateway</option>
        </select>
        <RelayCheckMap
          detail={detail}
          selected={selected}
          onSelect={setSelected}
          onReceiver={onReceiver}
        />
        <p className="relay-map-legend">
          <span className="relay-map-gateway receiver">R</span> receiver{" "}
          <span className="relay-map-gateway cohearer">C</span> co-hearer{" "}
          <span className="relay-map-sender now">S</span> sender{" "}
          <span className="relay-map-node left">left</span>{" "}
          <span className="relay-map-node out">ruled out</span>{" "}
          <span className="relay-map-node left estimated">estimated</span> ·
          blue line: the route · dashed circle: rule-out reach · solid: naming
          reach (route&apos;s relay blue, named green, clicked yellow)
        </p>
        <p className="relay-map-move">
          {updating ? <span className="muted">Updating… </span> : null}
          {detail.moved ? (
            <button
              type="button"
              className="secondary-button"
              onClick={() => onReceiver(null)}
            >
              Reset receiver position
            </button>
          ) : (
            "Click the map, or drag R, to place a survey receiver that heard the same copy."
          )}
        </p>
      </section>

      <section aria-labelledby="relay-candidates-heading">
        <div className="section-intro compact">
          <h3 id="relay-candidates-heading">
            1. Candidates: {candidates.length} known node
            {candidates.length === 1 ? "" : "s"} send byte{" "}
            {byteText(check.relay_byte)}
          </h3>
          <p>
            A red cell is a rule that removes the candidate. Position, role,
            spreading factor and firmware need no history, so after one of them
            the rest are not checked. {left.length} left.
          </p>
        </div>
        <CandidateTable
          detail={detail}
          routeKey={routeKey}
          selected={selected}
          onSelect={setSelected}
        />
      </section>

      <section aria-labelledby="relay-heard-heading">
        <div className="section-intro compact">
          <h3 id="relay-heard-heading">2. Heard directly (packet page only)</h3>
          <p>
            The packet page names the only candidate this gateway heard directly
            within {detail.rules.heard_days} days of the packet, whatever the
            rule-outs say. Survey measurements skip this step.
          </p>
        </div>
        <p
          className={`relay-step-result ${heard.length === 1 ? "decides" : ""}`}
        >
          {detail.receiver_mode === "survey"
            ? "The survey receiver has no direct links: nothing to read."
            : heard.length === 0
              ? "It heard none directly: the naming steps decide."
              : heard.length === 1
                ? `It heard only ${candidateName(heard[0])} directly (${heard[0].heard_directly} times): the packet page names it.`
                : `It heard ${heard.length} directly (${heard
                    .map(
                      (candidate) =>
                        `${candidateName(candidate)} ${candidate.heard_directly}`,
                    )
                    .join(", ")}): not one, so the naming steps decide.`}
        </p>
      </section>

      <section aria-labelledby="relay-steps-heading">
        <div className="section-intro compact">
          <h3 id="relay-steps-heading">
            3. Naming steps over the {left.length} left
          </h3>
          <p>
            The first step that names a candidate decides. A step names one only
            within its naming reach: the receiver no farther than its own reach
            or {detail.rules.name_km} km.
          </p>
        </div>
        {left.length === 0 ? (
          <p className="relay-step-result">
            No candidate is left: nothing is named.
          </p>
        ) : (
          <ol className="relay-steps">
            {detail.steps.map((step, stepIndex) => (
              <StepItem
                key={step.rule}
                step={step}
                detail={detail}
                left={left}
                byKey={byKey}
                state={
                  decidingIndex === stepIndex
                    ? "decides"
                    : decidingIndex !== -1 && stepIndex > decidingIndex
                      ? "after"
                      : "none"
                }
                decidedBy={
                  decidingIndex === -1
                    ? null
                    : STEP_LABELS[detail.steps[decidingIndex].rule]
                }
                name={name}
              />
            ))}
          </ol>
        )}
      </section>

      <section aria-labelledby="relay-reports-heading">
        <div className="section-intro compact">
          <h3 id="relay-reports-heading">4. Every gateway&apos;s report</h3>
          <p>
            Co-hearers heard the same rebroadcast: same relay byte and hops
            left. Each traceroute report names its own last relay.
          </p>
        </div>
        <ReportTable detail={detail} />
      </section>
    </>
  );
}

function Answered({
  node,
  step,
  outcome,
}: {
  node: CheckNode | null;
  step: StepName | null;
  outcome: Outcome | null;
}) {
  return (
    <>
      {node === null ? (
        <span className="muted">none</span>
      ) : (
        <NodeRef node={node} />
      )}
      {step !== null ? (
        <span className="muted"> by {STEP_LABELS[step].toLowerCase()}</span>
      ) : null}{" "}
      {outcome !== null ? (
        <Badge tone={OUTCOME_TONES[outcome]}>{outcome}</Badge>
      ) : null}
    </>
  );
}

function RouteChain({ detail }: { detail: RelayCheckDetail }) {
  if (detail.route.length === 0) return null;
  const gateway = detail.check.gateway.node_id;
  const relay = detail.check.route_relay?.node_id ?? null;
  return (
    <p className="relay-route">
      {detail.route.map((node, index) => (
        <span key={index}>
          {index > 0 ? (
            <span className="relay-route-hop">
              {" "}
              →{" "}
              {detail.route_snr_db[index - 1] !== undefined &&
              detail.route_snr_db[index - 1] !== null
                ? `${detail.route_snr_db[index - 1]} dB `
                : ""}
            </span>
          ) : null}
          <span
            className={
              node !== null &&
              (node.node_id === gateway || node.node_id === relay)
                ? "relay-route-mark"
                : undefined
            }
          >
            {node === null ? "?" : nodeText(node)}
          </span>
        </span>
      ))}
    </p>
  );
}

function candidateName(candidate: RelayCandidate | undefined): string {
  if (candidate === undefined) return "—";
  if (candidate.node_id === null) return "an opted-out node";
  return (
    candidate.name ?? candidate.short_name ?? formatNodeId(candidate.node_id)
  );
}

function Cell({
  hit,
  children,
}: {
  /** The rule this cell shows removed the candidate. */
  hit: boolean;
  children: ReactNode;
}) {
  return <td className={hit ? "rule-hit" : undefined}>{children}</td>;
}

function CandidateTable({
  detail,
  routeKey,
  selected,
  onSelect,
}: {
  detail: RelayCheckDetail;
  routeKey: number | null;
  selected: number | null;
  onSelect: (key: number) => void;
}) {
  const [allShown, setAllShown] = useState(false);
  const rules = detail.rules;
  const copySf = detail.spreading_factor;
  const order = (candidate: RelayCandidate) => [
    candidate.key === routeKey ? 0 : 1,
    candidate.ruled_out.length === 0 ? 0 : 1,
    candidate.receiver_km ?? Infinity,
  ];
  const sorted = [...detail.candidates].sort((a, b) => {
    const [x, y] = [order(a), order(b)];
    return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
  });
  const STATIC = ["unplaced", "client_mute", "channel", "preset", "firmware"];
  const early = (candidate: RelayCandidate) =>
    candidate.key !== routeKey &&
    candidate.ruled_out.some((rule) => STATIC.includes(rule));
  const hiddenCount = sorted.filter(early).length;
  const rows = allShown
    ? sorted
    : sorted.filter((candidate) => !early(candidate));
  const has = (candidate: RelayCandidate, rule: RuleOut) =>
    candidate.ruled_out.includes(rule);
  const checked = (candidate: RelayCandidate) =>
    !candidate.ruled_out.some((rule) => STATIC.includes(rule));
  return (
    <>
      <WideTable>
        <table className="relay-candidate-table">
          <thead>
            <tr>
              <th>Candidate</th>
              <th>Result</th>
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
                <span className="column-note">
                  ≤ reach or {rules.rule_out_km} km
                </span>
              </th>
              <th>
                Own report
                <span className="column-note">
                  hops left: copy&apos;s or one more
                </span>
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
              <th>
                Naming reach
                <span className="column-note">
                  ≤ reach or {rules.name_km} km
                </span>
              </th>
              <th className="number">Heard directly</th>
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
                <td>
                  {candidate.ruled_out.length === 0 ? (
                    <Badge tone="green">left</Badge>
                  ) : (
                    <Badge tone="neutral">ruled out</Badge>
                  )}
                </td>
                <Cell hit={has(candidate, "unplaced")}>
                  {candidate.latitude === null && candidate.node_id !== null
                    ? has(candidate, "unplaced")
                      ? "none"
                      : "—"
                    : candidate.estimated
                      ? "estimated"
                      : "now"}
                </Cell>
                <Cell hit={has(candidate, "client_mute")}>
                  {candidate.role ?? "—"}
                </Cell>
                <Cell
                  hit={has(candidate, "channel") || has(candidate, "preset")}
                >
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
                </Cell>
                <Cell hit={has(candidate, "firmware")}>
                  {firmwareText(candidate.firmware)}
                </Cell>
                <Cell hit={has(candidate, "receiver_far")}>
                  {checked(candidate) ? (
                    <KmOf
                      km={candidate.receiver_km}
                      limit={Math.max(candidate.reach_km, rules.rule_out_km)}
                    />
                  ) : (
                    <NotChecked />
                  )}
                </Cell>
                <Cell hit={has(candidate, "own_report")}>
                  {!checked(candidate) ? (
                    <NotChecked />
                  ) : candidate.own_hop_limit === null ? (
                    <span className="muted">no report</span>
                  ) : (
                    `${candidate.own_hop_limit} left`
                  )}
                </Cell>
                <Cell hit={has(candidate, "silent")}>
                  {!checked(candidate) ? (
                    <NotChecked />
                  ) : (
                    <ActivityText candidate={candidate} />
                  )}
                </Cell>
                <Cell hit={has(candidate, "sender_far")}>
                  {!checked(candidate) || !detail.one_hop ? (
                    <span className="muted">—</span>
                  ) : (
                    <KmOf
                      km={candidate.sender_km}
                      limit={Math.max(candidate.reach_km, rules.rule_out_km)}
                    />
                  )}
                </Cell>
                <td>
                  {candidate.ruled_out.length > 0 ? (
                    <span className="muted">—</span>
                  ) : (
                    <>
                      {candidate.within_naming_reach ? (
                        "within"
                      ) : (
                        <span className="rule-miss">beyond</span>
                      )}
                      <span className="muted">
                        {" "}
                        ({formatKm(candidate.receiver_km)} of{" "}
                        {formatKm(Math.max(candidate.reach_km, rules.name_km))})
                      </span>
                    </>
                  )}
                </td>
                <td className="number">
                  {candidate.heard_directly > 0
                    ? candidate.heard_directly
                    : "—"}
                </td>
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
            ? "Hide those ruled out before any distance check"
            : `Show ${hiddenCount} more ruled out before any distance check`}
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

/** How the receiver, the sender and the candidates were placed. */
function PlacementNotes({ detail }: { detail: RelayCheckDetail }) {
  const { receiver, sender, sender_when_heard: heard } = detail;
  const moved =
    sender !== null && heard !== null ? kmBetween(sender, heard) : null;
  return (
    <p>
      Receiver{" "}
      {receiver === null
        ? "unplaced: no rule that needs its position applies."
        : receiver.placed_by === "chosen"
          ? "placed on this page."
          : detail.receiver_mode === "survey"
            ? `at ${nodeText(detail.check.gateway)}'s position (${PLACED_BY[receiver.placed_by]}).`
            : `${nodeText(detail.check.gateway)}: ${PLACED_BY[receiver.placed_by]}.`}{" "}
      The rules place the candidates and the sender where they are now, else
      estimated from their neighbours (hollow).{" "}
      {sender === null
        ? "The sender is unplaced."
        : heard === null
          ? `The sender is ${PLACED_BY[sender.placed_by]}; it reported no position by the packet.`
          : moved !== null && moved >= 0.5
            ? `The sender is ${PLACED_BY[sender.placed_by]}, ${formatKm(moved)} km from where it was when heard (dotted line).`
            : `The sender is ${PLACED_BY[sender.placed_by]}, which is where it was when heard.`}
    </p>
  );
}

function kmBetween(
  a: { latitude: number; longitude: number },
  b: { latitude: number; longitude: number },
): number {
  const rad = Math.PI / 180;
  const dLat = (b.latitude - a.latitude) * rad;
  const dLon = (b.longitude - a.longitude) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a.latitude * rad) *
      Math.cos(b.latitude * rad) *
      Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

function NotChecked() {
  return <span className="muted">not checked</span>;
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

function StepItem({
  step,
  detail,
  left,
  byKey,
  state,
  decidedBy,
  name,
}: {
  step: RelayCheckStep;
  detail: RelayCheckDetail;
  left: RelayCandidate[];
  byKey: Map<number, RelayCandidate>;
  /** decides: this step names the relay; after: an earlier one did. */
  state: "decides" | "after" | "none";
  decidedBy: string | null;
  name: (key: number | null) => string;
}) {
  const label = STEP_LABELS[step.rule];
  return (
    <li className={`relay-step relay-step-${state}`}>
      <h4>
        {label}{" "}
        {state === "decides" ? (
          <Badge tone="green">names {name(step.named)}</Badge>
        ) : state === "after" ? (
          <Badge tone="neutral">after {decidedBy}</Badge>
        ) : (
          <Badge tone="neutral">names none</Badge>
        )}
      </h4>
      <p className="relay-step-rule">{STEP_RULES[step.rule](detail)}</p>
      <StepFigures step={step} detail={detail} left={left} byKey={byKey} />
      <p
        className={`relay-step-result ${state === "decides" ? "decides" : ""}`}
      >
        {stepResult(step, detail, left, byKey, name)}
        {state === "after" && step.named !== null
          ? " Not used: an earlier step decided."
          : ""}
      </p>
    </li>
  );
}

const STEP_RULES: Record<
  RelayCheckStep["rule"],
  (detail: RelayCheckDetail) => string
> = {
  signal: (detail) =>
    `Each co-hearer that does not send the byte reported this copy's SNR; each candidate's usual direct SNR at that gateway (7 days before) says how likely it is to be the relay. A gateway that never heard a candidate counts against it. The best names one when it beats the next candidate and a node no co-hearer hears by ${detail.rules.score_margin} or more.`,
  gateway_link: () =>
    "The candidate with the most radio links with any one co-hearer (direct receptions and neighbour reports).",
  sender_link: () =>
    "After one hop only: the candidate with the most radio links and traceroute hops with the sender.",
  reported: () =>
    "A gateway sending the byte that reported the packet with one more hop left rebroadcast it, though the receiver may have heard another copy. It names one when it is the only such candidate within its naming reach.",
  only_candidate: () => "The one candidate left, if within its naming reach.",
};

function StepFigures({
  step,
  detail,
  left,
  byKey,
}: {
  step: RelayCheckStep;
  detail: RelayCheckDetail;
  left: RelayCandidate[];
  byKey: Map<number, RelayCandidate>;
}) {
  const scores = new Map(step.scores.map((score) => [score.key, score.score]));
  const routeKey = detail.check.route_relay?.node_id ?? null;
  if (step.rule === "signal") {
    if (scores.size === 0) return null;
    const copies = detail.reports.filter((report) => report.signal);
    const profile = new Map(
      detail.signals.map((signal) => [
        `${signal.key}/${signal.gateway_node_id}`,
        signal,
      ]),
    );
    const rows = [...left].sort(
      (a, b) => (scores.get(b.key) ?? 0) - (scores.get(a.key) ?? 0),
    );
    return (
      <WideTable>
        <table className="relay-step-table">
          <thead>
            <tr>
              <th>Candidate</th>
              {copies.map((report) => (
                <th key={report.reception_id}>
                  {nodeText(report.gateway)}
                  <span className="column-note">
                    copy{" "}
                    {report.snr_db === null ? "no SNR" : `${report.snr_db} dB`}
                  </span>
                </th>
              ))}
              <th className="number">Score</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <tr
                key={candidate.key}
                className={
                  candidate.key === step.pick ? "step-pick-row" : undefined
                }
              >
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
                        <span className="rule-miss">never heard</span>
                      ) : signal.snr_mean_db === null ? (
                        `heard, no SNR`
                      ) : (
                        <>
                          {signal.snr_mean_db.toFixed(1)} dB
                          <span className="muted"> ({signal.snr_count})</span>
                        </>
                      )}
                    </td>
                  );
                })}
                <td className="number">
                  {scores.get(candidate.key)?.toFixed(1)}
                </td>
              </tr>
            ))}
            <tr className="muted">
              <th scope="row">A node no co-hearer hears</th>
              {copies.map((report) => (
                <td key={report.reception_id}>never heard</td>
              ))}
              <td className="number">{step.baseline?.toFixed(1)}</td>
            </tr>
          </tbody>
        </table>
      </WideTable>
    );
  }
  if (step.rule === "gateway_link" || step.rule === "sender_link") {
    if (scores.size === 0) return null;
    const others =
      step.rule === "gateway_link"
        ? detail.reports
            .filter((report) => report.kind === "cohearer")
            .map((report) => report.gateway)
        : [detail.check.sender];
    const links = new Map(
      detail.links.map((link) => [`${link.key}/${link.node_id}`, link]),
    );
    const rows = [...left].sort(
      (a, b) => (scores.get(b.key) ?? 0) - (scores.get(a.key) ?? 0),
    );
    return (
      <WideTable>
        <table className="relay-step-table">
          <thead>
            <tr>
              <th>Candidate</th>
              {others.map((other) => (
                <th key={other.node_id}>
                  {nodeText(other)}
                  {step.rule === "sender_link" ? (
                    <span className="column-note">radio + traceroute</span>
                  ) : null}
                </th>
              ))}
              <th className="number">Most</th>
              <th>Naming reach</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <tr
                key={candidate.key}
                className={
                  candidate.key === step.pick ? "step-pick-row" : undefined
                }
              >
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
                        : step.rule === "sender_link"
                          ? `${link.radio} + ${link.traceroute}`
                          : link.radio}
                    </td>
                  );
                })}
                <td className="number">{scores.get(candidate.key) ?? 0}</td>
                <td>
                  {candidate.within_naming_reach ? (
                    "within"
                  ) : (
                    <span className="rule-miss">beyond</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </WideTable>
    );
  }
  if (step.rule === "reported") {
    const earlier = detail.reports.filter(
      (report) => report.kind === "earlier" && report.sends_byte,
    );
    if (earlier.length === 0) return null;
    return (
      <ul className="relay-step-list">
        {earlier.map((report) => {
          const candidate =
            report.gateway.node_id === null
              ? undefined
              : byKey.get(report.gateway.node_id);
          return (
            <li key={report.reception_id}>
              {nodeText(report.gateway)} reported it with{" "}
              {report.hop_limit === 1 ? "1 hop" : `${report.hop_limit} hops`}{" "}
              left:{" "}
              {candidate === undefined
                ? "not a candidate"
                : candidate.ruled_out.length > 0
                  ? "ruled out"
                  : candidate.within_naming_reach
                    ? "left, within its naming reach"
                    : "left, beyond its naming reach"}
            </li>
          );
        })}
      </ul>
    );
  }
  return null;
}

function stepResult(
  step: RelayCheckStep,
  detail: RelayCheckDetail,
  left: RelayCandidate[],
  byKey: Map<number, RelayCandidate>,
  name: (key: number | null) => string,
): string {
  const picked =
    step.pick === null
      ? ""
      : step.named !== null
        ? `${name(step.pick)}, within its naming reach: named.`
        : `${name(step.pick)}, but beyond its naming reach (${formatKm(byKey.get(step.pick)?.receiver_km ?? null)} km).`;
  switch (step.rule) {
    case "signal": {
      if (step.scores.length === 0)
        return "No co-hearer that does not send the byte reported this copy: nothing to compare.";
      const ranked = [...step.scores].sort((a, b) => b.score - a.score);
      const best = ranked[0];
      const runnerUp = ranked.length > 1 ? ranked[1] : null;
      const baseline = step.baseline ?? -Infinity;
      const rivalIsUnknown = runnerUp === null || baseline >= runnerUp.score;
      const rival = rivalIsUnknown ? baseline : runnerUp.score;
      const margin = best.score - rival;
      const against = rivalIsUnknown
        ? `a node no co-hearer hears (${baseline.toFixed(1)})`
        : `${name(runnerUp.key)} (${runnerUp.score.toFixed(1)})`;
      return `Best ${name(best.key)} at ${best.score.toFixed(1)}, against ${against}: margin ${margin.toFixed(1)}, needs ${detail.rules.score_margin}. ${step.pick !== null ? picked : "No name."}`;
    }
    case "gateway_link":
      if (step.scores.length === 0) return "No co-hearers: nothing to read.";
      if (step.pick === null)
        return "No candidate left has a radio link with a co-hearer.";
      return `Most links: ${picked}`;
    case "sender_link":
      if (step.scores.length === 0)
        return "More than one hop used: the relay need not have heard the sender.";
      if (step.pick === null)
        return "No candidate left has a radio link or traceroute hop with the sender.";
      return `Most links: ${picked}`;
    case "reported": {
      const reporters = step.scores.filter((score) => score.score > 0);
      if (reporters.length === 0)
        return "No candidate left reported the packet with one more hop left.";
      if (step.named !== null) return `${name(step.named)}: named.`;
      const within = reporters.filter(
        (score) => byKey.get(score.key)?.within_naming_reach,
      );
      return within.length === 0
        ? `${reporters.map((score) => name(score.key)).join(", ")} did, beyond naming reach.`
        : `${within.length} within naming reach did: not one.`;
    }
    case "only_candidate":
      return left.length === 1
        ? picked
        : `${left.length} candidates are left: not one.`;
  }
}

function ReportTable({ detail }: { detail: RelayCheckDetail }) {
  const KIND_ORDER = ["receiver", "cohearer", "earlier", "other", "sender"];
  const KIND_LABELS: Record<RelayCheckReport["kind"], string> = {
    receiver: "this copy",
    cohearer: "co-hearer",
    earlier: "one more hop left",
    other: "other",
    sender: "sender's own upload",
  };
  const relay = detail.check.route_relay?.node_id ?? null;
  const reports = [...detail.reports].sort(
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
            <th>Its route&apos;s relay</th>
          </tr>
        </thead>
        <tbody>
          {reports.map((report) => {
            const differs =
              report.kind === "cohearer" &&
              report.route_relay !== null &&
              relay !== null &&
              report.route_relay.node_id !== relay;
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
                    <span className="muted"> · signal step</span>
                  ) : null}
                  {detail.receiver_mode === "survey" &&
                  report.gateway.node_id === detail.check.gateway.node_id ? (
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
                <td className={differs ? "rule-hit" : undefined}>
                  <NodeRef node={report.route_relay} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </WideTable>
  );
}
