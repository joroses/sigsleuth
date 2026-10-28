import { useEffect, useMemo, useState } from "react";

import {
  RelayAnswer,
  RelayCandidate,
  RelayCheck,
  RelayCheckDetail,
  RelayCheckList,
  RelayCheckReport,
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

type Answer = "survey" | "page";
type Outcome = NonNullable<RelayAnswer["outcome"]>;
type Show = Outcome | "not_right" | "all";
type How = NonNullable<RelayAnswer["how"]>;
type Term = RelayCandidate["terms"][number]["term"];
type CheckNode = RelayCheck["sender"];

const ANSWERS: { value: Answer; label: string }[] = [
  { value: "survey", label: "Survey receiver (no links of its own)" },
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
// Candidates the score table shows before "Show all".
const SCORE_ROWS = 8;

const HOW_LABELS: Record<How, string> = {
  heard_directly: "Heard directly",
  receiver_heard: "Heard by the gateway",
  gateway_link: "Links with co-hearers",
  sender_link: "Links with the sender",
  reported: "Reported one hop earlier",
  only_candidate: "Distance",
};

const WHY_LABELS: Record<
  Exclude<RelayAnswer["why"], "heard_directly" | "named">,
  string
> = {
  below: "Likeliest not sure enough",
  unknown: "An unknown node likeliest",
  no_evidence: "Likeliest has no evidence",
  no_candidates: "No candidate",
};

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
};

const OUTCOME_TONES = {
  right: "green",
  wrong: "red",
  undecided: "amber",
} as const;

/**
 * Diagnostics' relay checks (#diagnostics/relays): traceroute copies whose
 * route names their relay, against the relay SigSleuth names, and one
 * copy's scores term by term (#diagnostics/relays/<reception>).
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

/** Why a check came out as it did, to group the list by. */
function whyOf(check: RelayCheck, answer: Answer): string {
  const given = check[answer];
  if (given.why === "heard_directly") return "Heard directly";
  if (given.why === "named" && given.how !== null) {
    return `Named by ${HOW_LABELS[given.how].toLowerCase()}`;
  }
  if (check.route_relay_possible === null) {
    return "Route's relay is no candidate";
  }
  if (!check.route_relay_possible) return "Route's relay cannot have sent it";
  return WHY_LABELS[given.why as keyof typeof WHY_LABELS];
}

function shows(outcome: Outcome | null, show: Show): boolean {
  if (show === "all") return true;
  if (show === "not_right") return outcome !== "right";
  return outcome === show;
}

function percent(probability: number | null): string {
  if (probability === null) return "—";
  const value = probability * 100;
  return `${value < 1 && value > 0 ? "<1" : Math.round(value)}%`;
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
    "survey",
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
  let likeliestRight = 0;
  let likeliestChecked = 0;
  for (const check of checks) {
    const given = check[answer];
    const outcome = given.outcome;
    if (outcome !== null) counts[outcome] += 1;
    const routeId = check.route_relay?.node_id;
    if (outcome !== null && routeId !== undefined && routeId !== null) {
      likeliestChecked += 1;
      if (given.likeliest?.node_id === routeId) likeliestRight += 1;
    }
  }
  const named = counts.right + counts.wrong;
  const total = named + counts.undecided;
  const rates = [
    {
      label: "Accuracy when named",
      title: "Named relay matches the route",
      right: counts.right,
      total: named,
    },
    {
      label: "Named",
      title: "Copies with a named relay",
      right: named,
      total,
    },
    {
      label: "Likeliest correct",
      title: "Top-scored candidate matches the route",
      right: likeliestRight,
      total: likeliestChecked,
    },
  ];
  const kept = checks.filter((check) => shows(check[answer].outcome, show));
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
            checked against the relay SigSleuth names. Open one to see each
            candidate&apos;s score, term by term.
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
          <dl className="relay-check-rates" aria-label="Loaded check rates">
            {rates.map((rate) => (
              <div key={rate.label}>
                <dt title={rate.title}>{rate.label}</dt>
                <dd>
                  {rate.total === 0
                    ? "—"
                    : `${((100 * rate.right) / rate.total).toFixed(1)}%`}{" "}
                  <span className="muted">
                    {rate.right} / {rate.total}
                  </span>
                </dd>
              </div>
            ))}
          </dl>
          <p className="relay-check-counts">
            {(["right", "wrong", "undecided"] as const).map((outcome) => (
              <Badge key={outcome} tone={OUTCOME_TONES[outcome]}>
                {counts[outcome]} {outcome}
              </Badge>
            ))}
            <span className="muted">
              {checks.length} loaded copies in {data.traceroutes} traceroutes
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
                    <th>Relay</th>
                    <th>Sender → gateway</th>
                    <th>Byte · hops</th>
                    <th
                      className="number"
                      title="Candidates that could have sent this copy"
                    >
                      Possible
                    </th>
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
  const given = check[answer];
  const relay = given.relay ?? given.likeliest;
  const differs =
    given.relay !== null &&
    given.likeliest !== null &&
    given.relay.node_id !== given.likeliest.node_id;
  return (
    <tr>
      <td>
        <a className="text-button" href={relayChecksHref(check.reception_id)}>
          <ShortTime value={check.received_at} />
        </a>
      </td>
      <td>
        {given.outcome !== null ? (
          <Badge tone={OUTCOME_TONES[given.outcome]}>{given.outcome}</Badge>
        ) : null}
        {check.mixed ? <MixedBadge /> : null}
      </td>
      <td>{whyOf(check, answer)}</td>
      <td>
        {check.route_relay === null ? "—" : nodeText(check.route_relay)}
        {check.route_snr_db !== null ? (
          <span className="muted"> {check.route_snr_db} dB</span>
        ) : null}
        {given.route_probability !== null ? (
          <span className="muted"> · {percent(given.route_probability)}</span>
        ) : null}
      </td>
      <td>
        {relay === null ? (
          <span className="muted">—</span>
        ) : (
          <>
            <Badge
              tone={
                given.outcome === null
                  ? "neutral"
                  : OUTCOME_TONES[given.outcome]
              }
              title={
                given.relay === null ? "Likeliest, not named" : "Named relay"
              }
            >
              {nodeText(relay)}
            </Badge>
            {!differs && given.likeliest !== null ? (
              <span className="muted"> {percent(given.probability)}</span>
            ) : null}
            {differs ? (
              <span className="column-note">
                Likeliest {nodeText(given.likeliest)}{" "}
                {percent(given.probability)}
              </span>
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
      <td className="number">{check.possible}</td>
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
        <CheckScores
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

function CheckScores({
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
  const possible = candidates.filter((candidate) => candidate.possible);
  const heard = candidates.filter((candidate) => candidate.heard_directly > 0);
  const routeKey = check.route_relay?.node_id ?? null;
  // What this receiver names: the survey answer, or the gateway's own.
  const given = detail.receiver_mode === "gateway" ? check.page : check.survey;
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
              <Answered answer={check.page} />
            </dd>
          </div>
          <div>
            <dt>Survey receiver names</dt>
            <dd>
              <Answered answer={check.survey} />
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
          <span className="relay-map-node left">with evidence</span>{" "}
          <span className="relay-map-node out">without</span>{" "}
          <span className="relay-map-node left estimated">estimated</span> ·
          blue line: the route · solid circle: reach or {detail.rules.name_km}{" "}
          km, within which counts for it · dashed: reach or{" "}
          {detail.rules.far_km} km, beyond which counts against it (route&apos;s
          relay blue, named green, clicked yellow)
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
            {possible.length} may have sent this copy. Firmware without the
            relay byte, or a map report on another spreading factor, rules out
            the rest; everything else only counts for or against a candidate.
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
            within {detail.rules.heard_days} days of the packet. When it heard
            more, each of them counts as heard by the gateway in its score.
            Survey measurements skip this.
          </p>
        </div>
        <p
          className={`relay-step-result ${heard.length === 1 ? "decides" : ""}`}
        >
          {detail.receiver_mode === "survey"
            ? "The survey receiver has no direct links: nothing to read."
            : heard.length === 0
              ? "It heard none directly: the score decides."
              : heard.length === 1
                ? `It heard only ${candidateName(heard[0])} directly (${heard[0].heard_directly} times): the packet page names it.`
                : `It heard ${heard.length} directly (${heard
                    .map(
                      (candidate) =>
                        `${candidateName(candidate)} ${candidate.heard_directly}`,
                    )
                    .join(", ")}): not one, so the score decides.`}
        </p>
      </section>

      <section aria-labelledby="relay-score-heading">
        <div className="section-intro compact">
          <h3 id="relay-score-heading">3. Scores</h3>
          <p>
            Each term times its weight adds to a candidate&apos;s score; a node
            SigSleuth does not know scores {detail.rules.unknown_score}. The
            scores give each a probability, and the likeliest is named at{" "}
            {Math.round(detail.rules.name_probability * 100)}% or more when one
            of its terms ties it to the copy: within reach, under 5 km, linked
            to a co-hearer or the sender, reported one hop earlier, heard by the
            gateway, or a matching signal.
          </p>
        </div>
        <p
          className={`relay-step-result ${given.relay !== null && given.why !== "heard_directly" ? "decides" : ""}`}
        >
          {scoreResult(given, detail)}
        </p>
        <ScoreTable detail={detail} routeKey={routeKey} />
        <SignalTable detail={detail} routeKey={routeKey} />
        <LinkTable detail={detail} routeKey={routeKey} />
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

function Answered({ answer }: { answer: RelayAnswer }) {
  return (
    <>
      {answer.relay === null ? (
        <span className="muted">none</span>
      ) : (
        <NodeRef node={answer.relay} />
      )}
      {answer.how !== null ? (
        <span className="muted">
          {" "}
          by {HOW_LABELS[answer.how].toLowerCase()}
        </span>
      ) : answer.likeliest !== null ? (
        <span className="muted">
          {" "}
          · likeliest {nodeText(answer.likeliest)} at{" "}
          {percent(answer.probability)}
        </span>
      ) : null}{" "}
      {answer.outcome !== null ? (
        <Badge tone={OUTCOME_TONES[answer.outcome]}>{answer.outcome}</Badge>
      ) : null}
    </>
  );
}

function scoreResult(given: RelayAnswer, detail: RelayCheckDetail): string {
  const likeliest = nodeText(given.likeliest);
  const at = percent(given.probability);
  const needs = percent(detail.rules.name_probability);
  switch (given.why) {
    case "heard_directly":
      return "Heard directly decides before the score.";
    case "named":
      return `${nodeText(given.relay)} at ${at}: named, worded by ${HOW_LABELS[given.how ?? "only_candidate"].toLowerCase()}.`;
    case "below":
      return `Likeliest ${likeliest} at ${at}, under ${needs}: no name.`;
    case "unknown":
      return `A node SigSleuth does not know is likelier (${percent(detail.unknown_probability)}) than ${likeliest} (${at}): no name.`;
    case "no_evidence":
      return `${likeliest} at ${at}, but nothing ties it to the copy: no name.`;
    case "no_candidates":
      return "No candidate may have sent it: no name.";
  }
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

/** Candidates by probability, those that cannot have sent the copy last. */
function byProbability(candidates: RelayCandidate[]): RelayCandidate[] {
  return [...candidates].sort(
    (a, b) =>
      (b.probability ?? -1) - (a.probability ?? -1) ||
      (a.receiver_km ?? Infinity) - (b.receiver_km ?? Infinity),
  );
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
  const sorted = byProbability(detail.candidates);
  const hidden = (candidate: RelayCandidate) =>
    candidate.key !== routeKey && !candidate.possible;
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
                <td className="number">
                  {candidate.possible ? (
                    percent(candidate.probability)
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

/** How the receiver, the sender and the candidates were placed. */
function PlacementNotes({ detail }: { detail: RelayCheckDetail }) {
  const { receiver, sender, sender_when_heard: heard } = detail;
  const moved =
    sender !== null && heard !== null ? kmBetween(sender, heard) : null;
  return (
    <p>
      Receiver{" "}
      {receiver === null
        ? "unplaced: no distance counts."
        : receiver.placed_by === "chosen"
          ? "placed on this page."
          : detail.receiver_mode === "survey"
            ? `at ${nodeText(detail.check.gateway)}'s position (${PLACED_BY[receiver.placed_by]}).`
            : `${nodeText(detail.check.gateway)}: ${PLACED_BY[receiver.placed_by]}.`}{" "}
      The score places the candidates and the sender where they are now, else
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

function ScoreTable({
  detail,
  routeKey,
}: {
  detail: RelayCheckDetail;
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
function SignalTable({
  detail,
  routeKey,
}: {
  detail: RelayCheckDetail;
  routeKey: number | null;
}) {
  const copies = detail.reports.filter((report) => report.signal);
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
      <h4 className="relay-score-figures">Signal at the co-hearers</h4>
      <WideTable>
        <table className="relay-step-table">
          <thead>
            <tr>
              <th>Candidate</th>
              {copies.map((report) => (
                <th key={report.reception_id}>
                  {nodeText(report.gateway)}
                  <span className="column-note">copy {report.snr_db} dB</span>
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
                      ) : signal.snr_mean_db === null ? (
                        "heard, no SNR"
                      ) : (
                        <>
                          {signal.snr_mean_db.toFixed(1)} dB
                          <span className="muted"> ({signal.snr_count})</span>
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
function LinkTable({
  detail,
  routeKey,
}: {
  detail: RelayCheckDetail;
  routeKey: number | null;
}) {
  if (detail.links.length === 0) return null;
  const sender = detail.check.sender.node_id;
  const others = [
    ...detail.reports
      .filter((report) => report.kind === "cohearer")
      .map((report) => report.gateway),
    ...(detail.one_hop ? [detail.check.sender] : []),
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
                    <span className="muted"> · signal term</span>
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
