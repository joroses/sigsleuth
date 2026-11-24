import { MouseEvent, useEffect, useMemo, useState } from "react";

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
  SHOW_UNKNOWN_FROM,
  SignalTable,
  WhyComparison,
} from "./relayScores";
import {
  HOW_LABELS,
  WHY_LABELS,
  byProbability,
  byteText,
  hopsText,
  nodeText,
  percent,
  radioSettings,
} from "./relayText";
import { useSetting, useViewState } from "./remember";
import { SurveyChecks } from "./SurveyChecks";
import { useApi, usePagedApi } from "./useApi";
import { Badge, ShortTime, StatusPanel, WideTable } from "./ui";

type Answer = "survey" | "page";
type Outcome = NonNullable<RelayAnswer["outcome"]>;
type Show = Outcome | "not_right" | "all";

const ANSWERS: {
  value: Answer;
  label: string;
  subtitle: string;
}[] = [
  {
    value: "survey",
    label: "Survey simulation",
    subtitle: "gateway hidden",
  },
  {
    value: "page",
    label: "Packet page",
    subtitle: "as reporting gateway",
  },
];
const SHOWS: { value: Show; label: string }[] = [
  { value: "not_right", label: "Wrong & undecided" },
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
  // List and detail share one active mode so opening a row keeps its mode.
  const [answer, setAnswer] = useSetting<Answer>(
    "relayChecks.answer",
    "survey",
    (value) => ANSWERS.some((item) => item.value === value),
  );
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
            answer={answer}
            onAnswer={setAnswer}
            onQueue={setQueue}
            onRefresh={() => setGeneration((value) => value + 1)}
          />
        </div>
      ) : null}
      {!surveys && receptionId !== null ? (
        <CheckDetail
          key={receptionId}
          receptionId={receptionId}
          mode={answer}
          onMode={setAnswer}
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

function modeCounts(checks: RelayCheck[], mode: Answer) {
  const counts = { right: 0, wrong: 0, undecided: 0 };
  for (const check of checks) {
    const outcome = check[mode].outcome;
    if (outcome !== null) counts[outcome] += 1;
  }
  const named = counts.right + counts.wrong;
  const total = named + counts.undecided;
  return { ...counts, named, total };
}

function CheckList({
  answer,
  onAnswer,
  onQueue,
  onRefresh,
}: {
  answer: Answer;
  onAnswer: (answer: Answer) => void;
  /** The checks listed, in order, for the open copy's previous and next. */
  onQueue: (ids: number[]) => void;
  onRefresh: () => void;
}) {
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
  const counts = modeCounts(checks, answer);
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

  const outcomeCounts: Record<Show, number> = {
    not_right: counts.wrong + counts.undecided,
    wrong: counts.wrong,
    undecided: counts.undecided,
    right: counts.right,
    all: counts.total,
  };

  return (
    <>
      <div className="section-intro compact relay-check-intro">
        <p>
          Traceroutes carry their own route, so SigSleuth uses them to benchmark
          both simulated Survey phones and Packet pages. Survey uploads are real
          phone measurements with no route truth.
        </p>
      </div>
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : data === null ? (
        <>
          <div className="nodes-toolbar">
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
            <button
              type="button"
              className="secondary-button"
              onClick={onRefresh}
            >
              Refresh
            </button>
          </div>
          <StatusPanel>Checking the newest {count} traceroutes…</StatusPanel>
        </>
      ) : (
        <>
          <div className="relay-mode-cards" role="group" aria-label="Mode">
            {ANSWERS.map((item) => {
              const stats = modeCounts(checks, item.value);
              const accuracy =
                stats.named === 0
                  ? "—"
                  : `${((100 * stats.right) / stats.named).toFixed(1)}%`;
              const namedRate =
                stats.total === 0
                  ? "—"
                  : `${((100 * stats.named) / stats.total).toFixed(1)}%`;
              return (
                <button
                  type="button"
                  key={item.value}
                  className="relay-mode-card"
                  aria-pressed={answer === item.value}
                  onClick={() => onAnswer(item.value)}
                >
                  <span className="relay-mode-card-title">
                    <strong>{item.label}</strong>
                    <span className="muted"> ({item.subtitle})</span>
                  </span>
                  <span className="relay-mode-card-stats">
                    {accuracy} accurate when named · {namedRate} named ·{" "}
                    {stats.wrong} wrong · {stats.undecided} undecided
                  </span>
                </button>
              );
            })}
          </div>
          <div className="nodes-toolbar">
            <div className="relay-outcome-pills" role="group" aria-label="Show">
              {SHOWS.map((item) => (
                <button
                  type="button"
                  key={item.value}
                  className="relay-why"
                  aria-pressed={show === item.value}
                  onClick={() => {
                    setShow(item.value);
                    setWhy(null);
                  }}
                >
                  {item.label} <strong>{outcomeCounts[item.value]}</strong>
                </button>
              ))}
            </div>
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
            <button
              type="button"
              className="secondary-button"
              onClick={onRefresh}
            >
              Refresh
            </button>
          </div>
          <p className="relay-check-counts">
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
                    <th>Byte</th>
                    <th>Route says</th>
                    <th>Named relay</th>
                    <th>Sender → gateway</th>
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
  const href = relayChecksHref(check.reception_id);
  const differs =
    given.relay !== null &&
    given.likeliest !== null &&
    given.relay.node_id !== given.likeliest.node_id;
  const ends = `${nodeText(check.sender)} → ${nodeText(check.gateway)}`;
  function openRow(event: MouseEvent) {
    if (
      (event.target as Element).closest("a") !== null ||
      window.getSelection()?.toString()
    ) {
      return;
    }
    window.location.hash = href;
  }
  return (
    <tr className="clickable-row" onClick={openRow}>
      <td>
        <a className="text-button" href={href}>
          <ShortTime value={check.received_at} />
        </a>
      </td>
      <td>
        {given.outcome !== null ? (
          <Badge tone={OUTCOME_TONES[given.outcome]}>{given.outcome}</Badge>
        ) : null}
        {check.mixed ? <MixedBadge /> : null}
      </td>
      <td className="relay-check-why">{whyOf(check, answer)}</td>
      <td>{byteText(check.relay_byte)}</td>
      <td>
        {check.route_relay === null ? "—" : nodeText(check.route_relay)}
        {check.route_snr_db !== null ? (
          <span className="muted"> {check.route_snr_db} dB</span>
        ) : null}
        {given.route_probability !== null ? (
          <span className="column-note">
            scored {percent(given.route_probability)}
          </span>
        ) : null}
      </td>
      <td>
        {given.relay === null ? (
          <>
            <span className="muted">None</span>
            {given.likeliest !== null ? (
              <span className="column-note">
                top: {nodeText(given.likeliest)} {percent(given.probability)}
              </span>
            ) : null}
          </>
        ) : (
          <>
            <Badge
              tone={
                given.outcome === null
                  ? "neutral"
                  : OUTCOME_TONES[given.outcome]
              }
              title="Named relay"
            >
              {nodeText(given.relay)}
            </Badge>
            {!differs && given.likeliest !== null ? (
              <span className="muted"> {percent(given.probability)}</span>
            ) : null}
            {differs && given.likeliest !== null ? (
              <span className="column-note">
                top: {nodeText(given.likeliest)} {percent(given.probability)}
              </span>
            ) : null}
          </>
        )}
      </td>
      <td className="relay-check-ends" title={ends}>
        {ends}
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
  mode,
  onMode,
  queue,
}: {
  receptionId: number;
  mode: Answer;
  onMode: (mode: Answer) => void;
  queue: number[];
}) {
  const [receiverAt, setReceiverAt] = useViewState<[number, number] | null>(
    "relayChecks.receiverAt",
    null,
  );
  const receiverMode = mode === "page" ? "gateway" : "survey";
  const state = useApi<RelayCheckDetail>(
    relayCheckUrl(
      receptionId,
      receiverMode === "survey" ? receiverAt : null,
      receiverMode,
    ),
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
          mode={mode}
          updating={state.loading}
          onReceiver={(at) => setReceiverAt(at)}
          onMode={(nextMode) => {
            setReceiverAt(null);
            onMode(nextMode);
          }}
        />
      )}
    </>
  );
}

function firmwareText(version: number | null): string {
  if (version === null) return "—";
  return `${Math.floor(version / 10000)}.${Math.floor(version / 100) % 100}.${version % 100}`;
}

function ineligibleReason(
  check: RelayCheck,
  candidates: RelayCheckDetail["candidates"],
): string | null {
  if (check.route_relay === null || check.route_relay_possible === true) {
    return null;
  }
  if (check.route_relay_possible === null) {
    return `True relay wasn’t scored: not in known nodes for byte ${byteText(check.relay_byte)}.`;
  }
  const routeCandidate = candidates.find(
    (candidate) => candidate.key === check.route_relay?.node_id,
  );
  if (
    routeCandidate?.firmware !== null &&
    routeCandidate?.firmware !== undefined &&
    routeCandidate.firmware < 20600
  ) {
    return `True relay wasn’t eligible: firmware ${firmwareText(routeCandidate.firmware)} predates relay bytes.`;
  }
  if (
    routeCandidate?.preset_spreading_factor !== null &&
    routeCandidate?.preset_spreading_factor !== undefined
  ) {
    return `True relay wasn’t eligible: map report uses ${radioSettings(routeCandidate.preset_spreading_factor, routeCandidate.preset_bandwidth_khz)}.`;
  }
  return "True relay wasn’t eligible to send this copy.";
}

function otherModeHint(check: RelayCheck, activeMode: Answer): string | null {
  const otherMode: Answer = activeMode === "survey" ? "page" : "survey";
  const current = check[activeMode];
  const other = check[otherMode];
  if (
    current.outcome === other.outcome &&
    current.relay?.node_id === other.relay?.node_id
  ) {
    return null;
  }
  const otherLabel =
    ANSWERS.find((item) => item.value === otherMode)?.label ?? otherMode;
  if (other.outcome === "right") {
    const how =
      other.how !== null ? ` (${HOW_LABELS[other.how].toLowerCase()})` : "";
    return `${otherLabel} got this right${how}`;
  }
  if (other.outcome === "wrong") {
    return `${otherLabel} got this wrong (${nodeText(other.relay)})`;
  }
  return `${otherLabel} left this undecided`;
}

function CheckScores({
  detail,
  mode,
  updating,
  onReceiver,
  onMode,
}: {
  detail: RelayCheckDetail;
  mode: Answer;
  updating: boolean;
  /** Moves the survey receiver; null resets its position. */
  onReceiver: (at: [number, number] | null) => void;
  onMode: (mode: Answer) => void;
}) {
  const [selected, setSelected] = useState<number | null>(null);
  const check = detail.check;
  const candidates = detail.candidates;
  const possible = candidates.filter((candidate) => candidate.possible);
  const possibleSorted = byProbability(possible);
  const heard = candidates.filter((candidate) => candidate.heard_directly > 0);
  const routeKey = check.route_relay?.node_id ?? null;
  const routeCandidate =
    routeKey === null
      ? undefined
      : candidates.find((candidate) => candidate.key === routeKey);
  const given = detail.receiver_mode === "gateway" ? check.page : check.survey;
  const activeMode: Answer =
    detail.receiver_mode === "gateway" ? "page" : "survey";
  const otherHint = otherModeHint(check, activeMode);
  const ineligible = ineligibleReason(check, candidates);
  const cohearersCount = detail.reports.filter(
    (report) => report.kind === "cohearer",
  ).length;
  const earlierCount = detail.reports.filter(
    (report) => report.kind === "earlier",
  ).length;
  const signalReports = detail.reports.filter(
    (report) =>
      report.signal ||
      (detail.receiver_mode === "gateway" && report.kind === "receiver"),
  );
  const signalGatewaysCount = new Set(
    detail.signals
      .filter((signal) =>
        signalReports.some(
          (report) => report.gateway.node_id === signal.gateway_node_id,
        ),
      )
      .map((signal) => signal.gateway_node_id),
  ).size;
  const linkedCandidatesCount = new Set(detail.links.map((link) => link.key))
    .size;

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
              ? ` (${radioSettings(detail.spreading_factor, detail.bandwidth_khz)})`
              : ""}{" "}
            · relay byte {byteText(check.relay_byte)} ·{" "}
            {check.hop_start === null || check.hop_limit === null
              ? "hops unknown"
              : `${check.hop_limit} of ${check.hop_start} hops left (${hopsText(check.hop_start, check.hop_limit)} used)`}
          </p>
        </div>
      </div>

      <div className="relay-mode-bar">
        <div className="relay-mode-switch" role="group" aria-label="Mode">
          {ANSWERS.map((item) => (
            <button
              type="button"
              key={item.value}
              className="relay-why"
              aria-pressed={mode === item.value}
              onClick={() => onMode(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        {updating ? <span className="muted">Updating…</span> : null}
        {otherHint !== null ? (
          <span className="muted">
            {otherHint} —{" "}
            <button
              type="button"
              className="text-button"
              onClick={() =>
                onMode(activeMode === "survey" ? "page" : "survey")
              }
            >
              switch
            </button>
          </span>
        ) : null}
      </div>

      <section className="relay-check-verdict" aria-label="Result">
        <dl className="relay-check-verdict-grid">
          <div>
            <dt>True relay (from route)</dt>
            <dd>
              <NodeRef node={check.route_relay} />
              {check.route_snr_db !== null ? (
                <span className="muted"> heard at {check.route_snr_db} dB</span>
              ) : null}
              <RouteChain detail={detail} />
            </dd>
          </div>
          <div>
            <dt>
              {detail.receiver_mode === "gateway"
                ? "Packet page guessed"
                : "Survey simulation guessed"}
            </dt>
            <dd>
              <Answered answer={given} />
              {given.relay === null ? (
                <span className="column-note">
                  {scoreResult(given, detail)}
                </span>
              ) : null}
            </dd>
          </div>
        </dl>
        {ineligible !== null ? (
          <p className="relay-check-warning muted">{ineligible}</p>
        ) : null}
        {check.mixed ? (
          <p className="relay-check-warning">
            <MixedBadge /> Gateways report different paths for copies with the
            same relay byte and hops left. Their signals may describe different
            rebroadcasts.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="relay-why-heading">
        <h3 id="relay-why-heading">Why it decided this</h3>
        {given.why === "heard_directly" ? (
          <p className="relay-step-result decides">
            Decided before scoring: gateway heard only{" "}
            <NodeRef node={given.relay} /> directly{" "}
            {heard[0]?.heard_directly ?? 1}× in the last{" "}
            {detail.rules.heard_days} days.
          </p>
        ) : (
          <>
            {detail.shortcut_skipped ? (
              <p className="relay-step-result">
                Direct-hearing shortcut skipped: copy RSSI is over 15 dB
                stronger than{" "}
                {heard[0] !== undefined
                  ? nodeText({ node_id: heard[0].node_id, name: heard[0].name })
                  : "the sole candidate"}
                ’s usual RSSI, so scoring decided.
              </p>
            ) : null}
            <WhyComparison
              detail={detail}
              top={possibleSorted[0]}
              compare={routeCandidate?.possible ? routeCandidate : undefined}
              topLabel={given.relay !== null ? "Named" : "Top"}
              compareLabel="Route relay"
              routeKey={routeKey}
              showGatewayHistory={detail.receiver_mode === "gateway"}
              showUnknown={
                given.why === "unknown" ||
                given.relay === null ||
                detail.unknown_probability >= SHOW_UNKNOWN_FROM
              }
              senderNodeId={check.sender.node_id}
              gatewayNodeId={check.gateway.node_id}
            />
          </>
        )}
      </section>

      <section aria-labelledby="relay-map-heading">
        <div className="section-intro compact">
          <h3 id="relay-map-heading">Where they are</h3>
          <PlacementNotes detail={detail} />
        </div>
        <RelayCheckMap
          detail={detail}
          viewKey={check.reception_id}
          routeKey={routeKey}
          namedKeys={[given.relay].flatMap((node) =>
            node?.node_id == null ? [] : [node.node_id],
          )}
          selected={selected}
          onSelect={setSelected}
          onReceiver={
            detail.receiver_mode === "survey" ? onReceiver : undefined
          }
        />
        <p className="relay-map-legend">
          <span className="relay-map-gateway receiver">R</span> receiver{" "}
          <span className="relay-map-gateway cohearer">C</span> co-hearer{" "}
          <span className="relay-map-sender now">S</span> sender{" "}
          <span className="relay-map-node left">with evidence</span>{" "}
          <span className="relay-map-node out">without</span>{" "}
          <span className="relay-map-node left estimated">estimated</span> ·
          blue line: route · {detail.rules.near_km} km circle around R:
          near-links radius · route&apos;s relay blue, named green, clicked
          yellow
        </p>
        <p className="relay-map-move">
          {detail.receiver_mode === "gateway" ? (
            "Switch to Survey simulation to move the receiver."
          ) : detail.moved ? (
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

      <section className="relay-details-list" aria-label="Advanced details">
        <details className="relay-details">
          <summary>
            Exact score terms &amp; all {candidates.length} candidates
            {possible.length < candidates.length
              ? ` (${possible.length} eligible)`
              : ""}
          </summary>
          <div className="relay-details-body">
            <ScoreTable detail={detail} routeKey={routeKey} />
            <CandidateTable
              detail={detail}
              routeKey={routeKey}
              selected={selected}
              onSelect={setSelected}
            />
          </div>
        </details>

        <details className="relay-details">
          <summary>
            Gateway reports ({detail.reports.length} total · {cohearersCount}{" "}
            co-hearer{cohearersCount === 1 ? "" : "s"} · {earlierCount} earlier)
          </summary>
          <div className="relay-details-body">
            <ReportTable
              reports={detail.reports}
              copyGateway={
                detail.receiver_mode === "survey" ? check.gateway.node_id : null
              }
            />
          </div>
        </details>

        {signalGatewaysCount > 0 ? (
          <details className="relay-details">
            <summary>
              Direct signal profiles ({signalGatewaysCount} gateway
              {signalGatewaysCount === 1 ? "" : "s"})
            </summary>
            <div className="relay-details-body">
              <SignalTable
                detail={detail}
                routeKey={routeKey}
                includeReceiver={detail.receiver_mode === "gateway"}
              />
            </div>
          </details>
        ) : null}

        {detail.links.length > 0 ? (
          <details className="relay-details">
            <summary>
              Co-hearer &amp; sender links ({linkedCandidatesCount} candidate
              {linkedCandidatesCount === 1 ? "" : "s"})
            </summary>
            <div className="relay-details-body">
              <LinkTable
                detail={detail}
                sender={check.sender}
                routeKey={routeKey}
              />
            </div>
          </details>
        ) : null}
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
  const receiverText =
    receiver === null
      ? "Receiver unplaced"
      : receiver.placed_by === "chosen"
        ? "Receiver placed on map"
        : detail.receiver_mode === "survey"
          ? `Receiver at ${nodeText(detail.check.gateway)}’s position (${PLACED_BY[receiver.placed_by]})`
          : `Receiver at ${nodeText(detail.check.gateway)} (${PLACED_BY[receiver.placed_by]})`;
  return (
    <p>
      {receiverText}; candidates and sender at current or estimated positions
      {moved !== null && moved >= 0.5
        ? `, sender moved ${formatKm(moved)} km since heard (dotted line).`
        : "."}
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
