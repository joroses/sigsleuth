import { useEffect, useMemo, useState } from "react";

import {
  SurveyCheck,
  SurveyCheckDetail,
  SurveyCheckList,
  SurveyLinkNode,
  surveyCheckUrl,
  surveyChecksUrl,
} from "./api";
import { formatKm, surveyChecksHref } from "./nodeDisplay";
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
  CheckNode,
  HOW_LABELS,
  WHY_LABELS,
  byProbability,
  byteText,
  hopsText,
  nodeText,
  percent,
} from "./relayText";
import { useSetting, useViewState } from "./remember";
import { useApi, usePagedApi } from "./useApi";
import { Badge, ShortTime, StatusPanel, WideTable } from "./ui";

type Show = "all" | "changed" | "unnamed";
type UploadHow = SurveyCheck["upload_how"];
type ShownHow = SurveyCheck["shown_how"];

const SHOWS: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "changed", label: "Changed since saved" },
  { value: "unnamed", label: "Unnamed on the map" },
];
const COUNTS = [25, 50, 100, 200];

const SURVEY_HOW_LABELS: Record<UploadHow | ShownHow, string> = {
  ...HOW_LABELS,
  direct: "Heard directly",
  survey_link: "Heard by a survey nearby",
  nearest: "Nearest, read as unknown",
  unknown: "Unknown",
};

const TODAY_WHY_LABELS: Record<SurveyCheck["today"]["why"], string> = {
  ...WHY_LABELS,
  direct: "Heard directly",
  named: "Named",
  no_relay_byte: "No relay byte",
};

/**
 * Relay checks of survey uploads (#diagnostics/relays/surveys): each relayed
 * measurement with the node saved for it and today's rules, and one
 * measurement's decisions explained
 * (#diagnostics/relays/surveys/<measurement>).
 */
export function SurveyChecks({
  measurementId,
}: {
  measurementId: number | null;
}) {
  const [listShown, setListShown] = useState(measurementId === null);
  if (measurementId === null && !listShown) setListShown(true);
  const [generation, setGeneration] = useState(0);
  const [queue, setQueue] = useState<number[]>([]);
  return (
    <>
      {listShown ? (
        <div hidden={measurementId !== null}>
          <SurveyList
            key={generation}
            onQueue={setQueue}
            onRefresh={() => setGeneration((value) => value + 1)}
          />
        </div>
      ) : null}
      {measurementId !== null ? (
        <SurveyDetail
          key={measurementId}
          measurementId={measurementId}
          queue={queue}
        />
      ) : null}
    </>
  );
}

const NOT_COUNTED =
  "Heard before its node moved, or old and its node not seen since";

/** Why the map shows what it shows, to group the list by. */
function whyOf(check: SurveyCheck): string {
  if (!check.counted) return "No longer on the map";
  if (check.upload_how === "direct") return "Heard directly";
  if (check.shown_node !== null) {
    return `Named: ${SURVEY_HOW_LABELS[check.shown_how].toLowerCase()}`;
  }
  if (!check.relay_byte) return "No relay byte";
  if (check.today.node !== null) return "Named today, not saved";
  return TODAY_WHY_LABELS[check.today.why];
}

function shows(check: SurveyCheck, show: Show): boolean {
  switch (show) {
    case "all":
      return true;
    case "changed":
      return check.changed;
    case "unnamed":
      return check.shown_node === null;
  }
}

function SurveyList({
  onQueue,
  onRefresh,
}: {
  /** The measurements listed, in order, for the open one's previous and next. */
  onQueue: (ids: number[]) => void;
  onRefresh: () => void;
}) {
  const [show, setShow] = useSetting<Show>(
    "surveyChecks.show",
    "all",
    (value) => SHOWS.some((item) => item.value === value),
  );
  const [count, setCount] = useSetting<number>(
    "surveyChecks.count",
    COUNTS[1],
    (value) => COUNTS.includes(value as number),
  );
  const [direct, setDirect] = useSetting<boolean>(
    "surveyChecks.direct",
    false,
    (value) => typeof value === "boolean",
  );
  const [why, setWhy] = useViewState<string | null>("surveyChecks.why", null);
  const state = usePagedApi<SurveyCheckList>(
    surveyChecksUrl(count, direct),
    (loaded, next) => ({
      ...next,
      checks: [...loaded.checks, ...next.checks],
      newest_at: loaded.newest_at,
      oldest_at: next.oldest_at ?? loaded.oldest_at,
    }),
  );
  const data = state.data;
  const checks = useMemo(() => data?.checks ?? [], [data]);
  const relayed = checks.filter((check) => check.upload_how !== "direct");
  const rates = [
    {
      label: "Named on the map",
      title: "Relayed measurements the map counts and names a node for",
      agree: relayed.filter(
        (check) => check.counted && check.shown_node !== null,
      ).length,
      total: relayed.length,
    },
    {
      label: "Today agrees with saved",
      title: "Today's rules name the node saved",
      agree: relayed.filter((check) => !check.changed).length,
      total: relayed.length,
    },
  ];
  const kept = checks.filter((check) => shows(check, show));
  const whys = new Map<string, number>();
  for (const check of kept) {
    const reason = whyOf(check);
    whys.set(reason, (whys.get(reason) ?? 0) + 1);
  }
  const listed =
    why === null || !whys.has(why)
      ? kept
      : kept.filter((check) => whyOf(check) === why);
  const queueKey = listed.map((check) => check.measurement_id).join(",");
  useEffect(() => {
    onQueue(queueKey === "" ? [] : queueKey.split(",").map(Number));
  }, [queueKey, onQueue]);

  return (
    <>
      <div className="section-intro compact relay-check-intro">
        <h3>Survey uploads</h3>
        <p>
          Relayed survey measurements and the node each heard, decided two ways:
          saved, from what MQTT and surveys knew at upload or when a later
          upload&apos;s direct receptions changed it; and by today&apos;s rules.
          The map shows the saved one. Surveys carry no route, so the rates
          below are agreement, not accuracy.
        </p>
      </div>
      <div className="nodes-toolbar">
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
          aria-label="Measurements per load"
          value={count}
          onChange={(event) => setCount(Number(event.target.value))}
        >
          {COUNTS.map((value) => (
            <option key={value} value={value}>
              {value} measurements
            </option>
          ))}
        </select>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={direct}
            onChange={(event) => setDirect(event.target.checked)}
          />
          Heard directly too
        </label>
        <button type="button" className="secondary-button" onClick={onRefresh}>
          Refresh
        </button>
      </div>
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : data === null ? (
        <StatusPanel>Checking the newest {count} measurements…</StatusPanel>
      ) : (
        <>
          <dl className="relay-check-rates" aria-label="Loaded agreement rates">
            {rates.map((rate) => (
              <div key={rate.label}>
                <dt title={rate.title}>{rate.label}</dt>
                <dd>
                  {rate.total === 0
                    ? "—"
                    : `${((100 * rate.agree) / rate.total).toFixed(1)}%`}{" "}
                  <span className="muted">
                    {rate.agree} / {rate.total}
                  </span>
                </dd>
              </div>
            ))}
          </dl>
          <p className="relay-check-counts">
            <span className="muted">
              {checks.length} loaded measurements
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
                    <th>Surveyor</th>
                    <th>Sender</th>
                    <th>Byte · hops</th>
                    <th>Signal</th>
                    <th>Saved</th>
                    <th>Today&apos;s rules</th>
                  </tr>
                </thead>
                <tbody>
                  {listed.map((check) => (
                    <SurveyRow key={check.measurement_id} check={check} />
                  ))}
                </tbody>
              </table>
            </WideTable>
          ) : (
            <StatusPanel>None of these measurements are shown.</StatusPanel>
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
                : `Check ${count} older measurements`}
            </button>
          ) : null}
        </>
      )}
    </>
  );
}

function signalText(snr: number | null, rssi: number | null): string {
  return `${snr ?? "—"} dB · ${rssi ?? "—"} dBm`;
}

function byteHops(check: SurveyCheck): string {
  return `${check.relay_byte ? byteText(check.relay_byte) : "—"} · ${hopsText(check.hop_start, check.hop_limit)}`;
}

/** A decision: its node and how, bold when the map shows it. */
function Decision({
  node,
  how,
  shown,
}: {
  node: CheckNode | null;
  how: UploadHow | ShownHow | null;
  shown: boolean;
}) {
  return (
    <span
      className={shown ? "survey-shown" : undefined}
      title={shown ? "What the map shows" : undefined}
    >
      {node === null ? <span className="muted">none</span> : nodeText(node)}
      {how !== null && node !== null ? (
        <span className="muted"> · {SURVEY_HOW_LABELS[how].toLowerCase()}</span>
      ) : null}
    </span>
  );
}

function SurveyRow({ check }: { check: SurveyCheck }) {
  const today = check.today;
  return (
    <tr>
      <td>
        <a
          className="text-button"
          href={surveyChecksHref(check.measurement_id)}
        >
          <ShortTime value={check.heard_at} />
        </a>
        {check.counted ? null : (
          <>
            {" "}
            <Badge tone="neutral" title={NOT_COUNTED}>
              not on the map
            </Badge>
          </>
        )}
      </td>
      <td>{nodeText(check.surveyor)}</td>
      <td>{nodeText(check.sender)}</td>
      <td>{byteHops(check)}</td>
      <td>{signalText(check.rx_snr, check.rx_rssi)}</td>
      <td>
        <Decision
          node={check.upload_node}
          how={check.upload_how}
          shown={check.counted}
        />
      </td>
      <td>
        <Decision node={today.node} how={today.how} shown={false} />
        {today.node === null && today.likeliest !== null ? (
          <span className="column-note">
            Likeliest {nodeText(today.likeliest)} {percent(today.probability)}
          </span>
        ) : null}
        {check.changed ? (
          <>
            {" "}
            <Badge tone="amber" title="Not the node saved">
              changed
            </Badge>
          </>
        ) : null}
      </td>
    </tr>
  );
}

function SurveyDetail({
  measurementId,
  queue,
}: {
  measurementId: number;
  queue: number[];
}) {
  const state = useApi<SurveyCheckDetail>(surveyCheckUrl(measurementId));
  const index = queue.indexOf(measurementId);
  const previous = index > 0 ? queue[index - 1] : null;
  const next = index >= 0 && index < queue.length - 1 ? queue[index + 1] : null;
  return (
    <>
      <nav className="relay-check-nav" aria-label="Survey checks">
        <a className="text-button" href={surveyChecksHref()}>
          ← Survey uploads
        </a>
        {index >= 0 ? (
          <span className="relay-check-step-through">
            {previous !== null ? (
              <a className="secondary-button" href={surveyChecksHref(previous)}>
                Previous
              </a>
            ) : null}
            <span className="muted">
              {index + 1} of {queue.length}
            </span>
            {next !== null ? (
              <a className="secondary-button" href={surveyChecksHref(next)}>
                Next
              </a>
            ) : null}
          </span>
        ) : null}
      </nav>
      {state.error !== null ? (
        <StatusPanel tone="error">{state.error}</StatusPanel>
      ) : state.data === null ? (
        <StatusPanel>Loading…</StatusPanel>
      ) : (
        <SurveyScores detail={state.data} />
      )}
    </>
  );
}

function radioText(detail: SurveyCheckDetail): string {
  const parts = [
    detail.spreading_factor === null ? null : `SF${detail.spreading_factor}`,
    detail.bandwidth_khz === null ? null : `${detail.bandwidth_khz} kHz`,
    detail.frequency_mhz === null ? null : `${detail.frequency_mhz} MHz`,
  ].filter((part) => part !== null);
  return parts.length === 0 ? "radio settings unknown" : parts.join(" · ");
}

function todayResult(detail: SurveyCheckDetail): string {
  const today = detail.check.today;
  const likeliest = nodeText(today.likeliest);
  const at = percent(today.probability);
  switch (today.why) {
    case "direct":
      return "The survey node heard the sender itself: nothing to score.";
    case "no_relay_byte":
      return "The packet carried no relay byte: nothing to score.";
    case "named":
      return `${nodeText(today.node)} at ${at}: named, worded by ${SURVEY_HOW_LABELS[today.how].toLowerCase()}.`;
    case "no_candidates":
      return "No candidate may have sent it: no name.";
    case "below":
      return `Likeliest ${likeliest} at ${at}, under ${percent(detail.rules.name_probability)}: no name.`;
    case "unknown":
      return `A node SigSleuth does not know is likelier (${percent(detail.unknown_probability)}) than ${likeliest} (${at}): no name.`;
    case "no_evidence":
      return `${likeliest} at ${at}, but nothing ties it to the copy: no name.`;
  }
}

function SurveyScores({ detail }: { detail: SurveyCheckDetail }) {
  const [selected, setSelected] = useState<number | null>(null);
  const check = detail.check;
  const named = [check.upload_node, check.today.node].flatMap((node) =>
    node?.node_id == null ? [] : [node.node_id],
  );
  const cohearers = detail.reports.filter(
    (report) => report.kind === "cohearer",
  );
  const earlierCount = detail.reports.filter(
    (report) => report.kind === "earlier",
  ).length;
  const scored = check.relay_byte !== null && check.upload_how !== "direct";
  const possible = detail.candidates.filter((candidate) => candidate.possible);
  const possibleSorted = byProbability(possible);
  const compareKey =
    (check.upload_how !== "nearest" ? check.upload_node?.node_id : null) ??
    null;
  const compareCandidate =
    compareKey === null
      ? undefined
      : possible.find((candidate) => candidate.key === compareKey);
  const signalGatewaysCount = new Set(
    detail.signals.map((signal) => signal.gateway_node_id),
  ).size;
  const linkedCandidatesCount = new Set(detail.links.map((link) => link.key))
    .size;
  return (
    <>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">Survey check</p>
          <h2>
            {nodeText(check.sender)} → {nodeText(check.surveyor)}
          </h2>
          <p className="diagnostic-lede">
            <ShortTime value={check.heard_at} />
            {detail.packet_row_id !== null ? (
              <>
                {" · "}
                <a
                  className="text-button"
                  href={`#packets/${detail.packet_row_id}`}
                >
                  packet
                </a>
              </>
            ) : null}{" "}
            · {radioText(detail)} · relay byte{" "}
            {check.relay_byte ? byteText(check.relay_byte) : "none"} ·{" "}
            {check.hop_start === null || check.hop_limit === null
              ? "hops unknown"
              : `${check.hop_limit} of ${check.hop_start} hops left (${hopsText(check.hop_start, check.hop_limit)} used)`}{" "}
            · {signalText(check.rx_snr, check.rx_rssi)}
          </p>
        </div>
      </div>

      <section className="relay-check-verdict" aria-label="Decisions">
        <dl>
          {check.counted ? null : (
            <div>
              <dt>On the map</dt>
              <dd>Not counted: {NOT_COUNTED.toLowerCase()}</dd>
            </div>
          )}
          <div>
            <dt>Saved</dt>
            <dd>
              <Decision
                node={check.upload_node}
                how={check.upload_how}
                shown={check.counted}
              />
            </dd>
          </div>
          <div>
            <dt>Today&apos;s rules</dt>
            <dd>
              <Decision
                node={check.today.node}
                how={check.today.how}
                shown={false}
              />{" "}
              {check.changed ? <Badge tone="amber">changed</Badge> : null}
            </dd>
          </div>
        </dl>
      </section>

      <section aria-labelledby="survey-why-heading">
        <h3 id="survey-why-heading">Why today&apos;s rules decided this</h3>
        <p
          className={`relay-step-result ${check.today.why === "named" ? "decides" : ""}`}
        >
          {todayResult(detail)}
        </p>
        {scored ? (
          <WhyComparison
            detail={detail}
            top={possibleSorted[0]}
            compare={compareCandidate}
            topLabel={check.today.node !== null ? "Named" : "Top"}
            compareLabel="Saved"
            routeKey={null}
            showGatewayHistory={false}
            showUnknown={
              check.today.why === "unknown" ||
              check.today.node === null ||
              detail.unknown_probability >= SHOW_UNKNOWN_FROM
            }
            senderNodeId={check.sender.node_id}
          />
        ) : null}
      </section>

      <section aria-labelledby="survey-map-heading">
        <div className="section-intro compact">
          <h3 id="survey-map-heading">Where they are</h3>
          <p>
            R is the centre of the square where the phone heard the packet;
            candidates and sender at current or estimated positions.
          </p>
        </div>
        <RelayCheckMap
          detail={detail}
          viewKey={check.measurement_id}
          routeKey={null}
          namedKeys={named}
          selected={selected}
          onSelect={setSelected}
        />
        <p className="relay-map-legend">
          <span className="relay-map-gateway receiver phone">R</span> phone{" "}
          <span className="relay-map-gateway cohearer">C</span> co-hearer{" "}
          <span className="relay-map-sender now">S</span> sender{" "}
          <span className="relay-map-node left">with evidence</span>{" "}
          <span className="relay-map-node out">without</span>{" "}
          <span className="relay-map-node left estimated">estimated</span> ·{" "}
          {detail.rules.near_km} km circle around R: near-links radius · named
          green, clicked yellow
        </p>
      </section>

      <section aria-labelledby="survey-link-heading">
        <div className="section-intro compact">
          <h3 id="survey-link-heading">Heard by surveys</h3>
          <p>
            Nodes sending byte{" "}
            {check.relay_byte ? byteText(check.relay_byte) : "—"} that any
            survey node on the same radio settings heard directly within{" "}
            {detail.link_rules.days} days. Those within {detail.link_rules.km}{" "}
            km score as heard nearby, unless they are muted (CLIENT_MUTE), which
            never rebroadcast.
          </p>
        </div>
        <SurveyLinkTable detail={detail} />
      </section>

      {scored ? (
        <section className="relay-details-list" aria-label="Advanced details">
          <details className="relay-details">
            <summary>
              Exact score terms &amp; all {detail.candidates.length} candidates
              {possible.length < detail.candidates.length
                ? ` (${possible.length} eligible)`
                : ""}
            </summary>
            <div className="relay-details-body">
              <ScoreTable detail={detail} routeKey={null} />
              <CandidateTable
                detail={detail}
                routeKey={null}
                selected={selected}
                onSelect={setSelected}
                heardColumn={false}
              />
            </div>
          </details>

          <details className="relay-details">
            <summary>
              MQTT reports ({detail.reports.length} total · {cohearers.length}{" "}
              co-hearer{cohearers.length === 1 ? "" : "s"} · {earlierCount}{" "}
              earlier)
            </summary>
            <div className="relay-details-body">
              {detail.reports.length > 0 ? (
                <ReportTable reports={detail.reports} routes={false} />
              ) : (
                <StatusPanel>No gateway reported this packet.</StatusPanel>
              )}
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
                  routeKey={null}
                  includeReceiver={false}
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
                  routeKey={null}
                />
              </div>
            </details>
          ) : null}
        </section>
      ) : null}
    </>
  );
}

function SurveyLinkTable({ detail }: { detail: SurveyCheckDetail }) {
  const nodes = detail.survey_link;
  if (nodes.length === 0) {
    return (
      <StatusPanel>
        No survey heard a node sending this byte directly.
      </StatusPanel>
    );
  }
  const saved = detail.check.upload_node;
  return (
    <WideTable>
      <table className="relay-step-table">
        <thead>
          <tr>
            <th>Node</th>
            <th className="number">Nearest km</th>
            <th className="number">Receptions</th>
            <th>
              Nearest in time
              <span className="column-note">
                within {detail.link_rules.km} km
              </span>
            </th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((node) => (
            <tr
              key={node.key}
              className={
                saved !== null && node.node_id === saved.node_id
                  ? "route-relay-row"
                  : undefined
              }
            >
              <th scope="row">
                <NodeRef node={{ node_id: node.node_id, name: node.name }} />
              </th>
              <td className="number">{formatKm(node.km)}</td>
              <td className="number">{node.receptions}</td>
              <td>{hoursText(node.hours)}</td>
              <td>{linkResult(node, detail)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </WideTable>
  );
}

function hoursText(hours: number | null): string {
  if (hours === null) return "—";
  const minutes = Math.round(Math.abs(hours) * 60);
  const text =
    minutes < 60
      ? `${minutes} min`
      : minutes < 48 * 60
        ? `${Math.round(minutes / 60)} h`
        : `${Math.round(minutes / 1440)} days`;
  return minutes === 0
    ? "same minute"
    : `${text} ${hours < 0 ? "before" : "after"}`;
}

function linkResult(node: SurveyLinkNode, detail: SurveyCheckDetail): string {
  if (node.excluded === "sender") return "the sender";
  if (node.excluded === "surveyor") return "the survey node";
  if (node.excluded === "muted") return "muted: never rebroadcasts";
  if (!node.candidate) {
    return node.km !== null && node.km <= detail.link_rules.km
      ? "cannot relay on these settings"
      : `not within ${detail.link_rules.km} km`;
  }
  return "heard nearby in the score";
}
