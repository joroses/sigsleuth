import { useEffect, useMemo, useState } from "react";

import {
  RelayAnswer,
  RelayCheck,
  RelayCheckDetail,
  RelayCheckList,
  relayCheckUrl,
  relayChecksUrl,
} from "./api";
import {
  PLACED_BY,
  formatKm,
  relayChecksHref,
  surveyChecksHref,
} from "./nodeDisplay";
import { RelayCheckMap } from "./RelayCheckMap";
import {
  CandidateTable,
  LinkTable,
  NodeRef,
  ReportTable,
  ScoreTable,
  SignalTable,
} from "./relayScores";
import {
  HOW_LABELS,
  WHY_LABELS,
  byteText,
  candidateName,
  hopsText,
  nodeText,
  percent,
} from "./relayText";
import { useSetting, useViewState } from "./remember";
import { SurveyChecks } from "./SurveyChecks";
import { useApi, usePagedApi } from "./useApi";
import { Badge, ShortTime, StatusPanel, WideTable } from "./ui";

type Answer = "survey" | "page";
type Outcome = NonNullable<RelayAnswer["outcome"]>;
type Show = Outcome | "not_right" | "all";

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

const OUTCOME_TONES = {
  right: "green",
  wrong: "red",
  undecided: "amber",
} as const;

/**
 * Diagnostics' relay checks tab (#diagnostics/relays): traceroute copies whose
 * route names their relay, against the relay SigSleuth names, and one
 * copy's scores term by term (#diagnostics/relays/<reception>); or survey
 * uploads (#diagnostics/relays/surveys).
 */
export function RelayChecks({
  receptionId,
  surveys = false,
  measurementId = null,
}: {
  receptionId: number | null;
  surveys?: boolean;
  measurementId?: number | null;
}) {
  // Each list loads once it is first shown, and stays loaded while the
  // other or an open item is shown, so going back to it is instant.
  const [listShown, setListShown] = useState(!surveys && receptionId === null);
  if (!surveys && receptionId === null && !listShown) setListShown(true);
  const [surveysOpened, setSurveysOpened] = useState(surveys);
  if (surveys && !surveysOpened) setSurveysOpened(true);
  const [generation, setGeneration] = useState(0);
  const [queue, setQueue] = useState<number[]>([]);
  return (
    <>
      <nav className="relay-check-kinds" aria-label="Relay checks of">
        <a href={relayChecksHref()} aria-current={surveys ? undefined : "page"}>
          Traceroute copies
        </a>
        <a
          href={surveyChecksHref()}
          aria-current={surveys ? "page" : undefined}
        >
          Survey uploads
        </a>
      </nav>
      {listShown ? (
        <div hidden={surveys || receptionId !== null}>
          <CheckList
            key={generation}
            onQueue={setQueue}
            onRefresh={() => setGeneration((value) => value + 1)}
          />
        </div>
      ) : null}
      {!surveys && receptionId !== null ? (
        <CheckDetail
          key={receptionId}
          receptionId={receptionId}
          queue={queue}
        />
      ) : null}
      {surveysOpened ? (
        <div hidden={!surveys}>
          <SurveyChecks measurementId={measurementId} />
        </div>
      ) : null}
    </>
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
      <div className="section-intro compact">
        <h3>Relay checks</h3>
        <p>
          Traceroute copies whose own route names their relay: the node before
          the gateway, when it sends the copy&apos;s relay byte. Each is checked
          against the relay SigSleuth names. Open one to see each
          candidate&apos;s score, term by term.
        </p>
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

function MixedBadge() {
  return (
    <Badge tone="amber" title="Reports carry different traceroute paths">
      mixed
    </Badge>
  );
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
            <MixedBadge /> Gateways report different paths for copies with the
            same relay byte and hops left. Their signals may describe different
            rebroadcasts.
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
          viewKey={check.reception_id}
          routeKey={routeKey}
          namedKeys={[check.page.relay, check.survey.relay].flatMap((node) =>
            node?.node_id == null ? [] : [node.node_id],
          )}
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
            within {detail.rules.heard_days} days of the packet, unless this
            copy is over 15 dB stronger than its usual RSSI. When it heard more,
            each of them counts as heard by the gateway in its score. Survey
            measurements skip this.
          </p>
        </div>
        <p
          className={`relay-step-result ${given.why === "heard_directly" ? "decides" : ""}`}
        >
          {detail.receiver_mode === "survey"
            ? "The survey receiver has no direct links: nothing to read."
            : detail.shortcut_skipped
              ? "The copy is over 15 dB stronger than the sole candidate’s usual RSSI: the score decides."
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
        <LinkTable
          detail={detail}
          sender={detail.check.sender}
          routeKey={routeKey}
        />
      </section>

      <section aria-labelledby="relay-reports-heading">
        <div className="section-intro compact">
          <h3 id="relay-reports-heading">4. Every gateway&apos;s report</h3>
          <p>
            Co-hearers heard the same rebroadcast: same relay byte and hops
            left. Each traceroute report names its own last relay.
          </p>
        </div>
        <ReportTable
          reports={detail.reports}
          copyGateway={
            detail.receiver_mode === "survey"
              ? detail.check.gateway.node_id
              : null
          }
        />
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
