/* eslint-disable local/hover-text -- long links explain on hover how each flag is decided */
import { ReactNode } from "react";

import { LongLink, LongLinkEnd, LongLinkRules, ReviewCheck } from "./api";
import {
  diagnosticsLinksHref,
  diagnosticsPacketsHref,
  formatKm,
  formatNodeId,
  lineOfSightHref,
  nodeName,
  tracerouteHopHref,
} from "./nodeDisplay";
import { SharedBadge } from "./radios";
import {
  Badge,
  DataItem,
  DoubtfulBadge,
  NodeLink,
  ShortTime,
  WideTable,
} from "./ui";

type Tone = "neutral" | "green" | "blue" | "amber" | "red";
type Shown = LongLink["shown"];
type Evidence = LongLink["evidence"][number];
type CheckKind = ReviewCheck["kind"];

type Factor = "shown" | "heard" | "ends" | "terrain" | "reach";

const SHOWN: Record<Shown, { label: string; tone: Tone }> = {
  long: { label: "still shown long", tone: "red" },
  shorter: { label: "now shorter", tone: "green" },
  hidden: { label: "hidden", tone: "neutral" },
};

function shownTitle(shown: Shown, rules: LongLinkRules): string {
  if (shown === "long") {
    return `The map draws it, or the Links page ranks it unbadged, within ${rules.moved_km} km of its longest distance`;
  }
  if (shown === "shorter") {
    return `Neither page shows it within ${rules.moved_km} km of its longest distance any more: an end moved or its position was corrected`;
  }
  return "The map leaves it out and the Links page ranks it apart as doubtful or unconfirmed";
}

/** What pages make of a long link now, or how many links they make that of. */
export function ShownBadge({
  shown,
  rules,
  count,
}: {
  shown: Shown;
  rules: LongLinkRules;
  count?: number;
}) {
  return (
    <Badge tone={SHOWN[shown].tone} title={shownTitle(shown, rules)}>
      {count === undefined ? null : `${count} `}
      {SHOWN[shown].label}
    </Badge>
  );
}

const KINDS: Record<
  Evidence["link_kind"],
  { label: string; tag: string; title: string }
> = {
  direct: {
    label: "Direct receptions",
    tag: "direct",
    title: "Times one end heard the other with no relay, both ways",
  },
  neighbor: {
    label: "Neighbour reports",
    tag: "neighbour",
    title: "Neighbour reports in which one end lists the other, both ways",
  },
  traceroute: {
    label: "Traceroute hops",
    tag: "traceroute",
    title: "Times a traceroute reported a step between them, both ways",
  },
};

const CHECKS: Record<CheckKind, { factor: Factor; label: string }> = {
  distance_range: { factor: "shown", label: "Distance changed while heard" },
  heard_once: { factor: "heard", label: "Heard only once" },
  traceroute_only: { factor: "heard", label: "Only traceroutes report it" },
  mqtt_hop: { factor: "heard", label: "Route steps at exactly 0 dB" },
  far_partners: { factor: "ends", label: "Far direct partners" },
  map_report: { factor: "ends", label: "Position from a map report" },
  stale_position: {
    factor: "ends",
    label: "Old position of a node that moved",
  },
  terrain_unchecked: { factor: "terrain", label: "Terrain not checked yet" },
  reach_long: { factor: "reach", label: "Unusually long for its ends" },
  reach_unknown: { factor: "reach", label: "Too little history to compare" },
};

function endName(end: LongLinkEnd): string {
  return nodeName(end.node) ?? formatNodeId(end.node.node_id);
}

function endOf(link: LongLink, nodeId: number): LongLinkEnd {
  return link.node_b.node.node_id === nodeId ? link.node_b : link.node_a;
}

function checkLabel(check: ReviewCheck, link: LongLink): string {
  const { label } = CHECKS[check.kind];
  return check.node_id === null
    ? label
    : `${label}: ${endName(endOf(link, check.node_id))}`;
}

function checkText(
  check: ReviewCheck,
  link: LongLink,
  rules: LongLinkRules,
): string {
  const end = check.node_id === null ? null : endOf(link, check.node_id);
  const name = end === null ? "An end" : endName(end);
  switch (check.kind) {
    case "distance_range":
      return `Heard from ${formatKm(link.shortest_km)} to ${formatKm(link.longest_km)} km apart, a change of over ${rules.moved_km} km: an end moved or its position jumped while it was heard.`;
    case "heard_once":
      return "Heard only once, so nothing else backs it up.";
    case "traceroute_only":
      return "Only traceroutes report it, with no direct reception or neighbour report, and a traceroute hop can cross an MQTT bridge.";
    case "mqtt_hop":
      return "Every traceroute step on it reports exactly 0 dB, the mark of a hop over MQTT rather than radio.";
    case "far_partners":
      return `${name}'s nearest of ${end?.direct_partners} direct partners is ${formatKm(end?.nearest_partner_km ?? null)} km away, over ${rules.local_partner_km} km: its position may be wrong.`;
    case "map_report":
      return `${name}'s position comes from a map report, which may be rounded on purpose.`;
    case "stale_position": {
      const reported = end?.position_observed_at ?? null;
      const hours =
        reported === null
          ? null
          : Math.round(
              (Date.parse(link.longest_at) - Date.parse(reported)) / 3_600_000,
            );
      return `${name} has moved before, and its position was ${hours === null ? `over ${rules.stale_position_hours} h` : `${hours} h`} old when this was longest: it may have travelled again.`;
    }
    case "terrain_unchecked":
      return "Not checked against terrain yet: the check runs every few minutes, once terrain data can be loaded.";
    case "reach_long": {
      const larger = largerThreshold(link);
      return `Its longest distance, ${formatKm(link.longest_km)} km, is over the larger review threshold, ${larger}: unusually long for these nodes.`;
    }
    case "reach_unknown":
      return "Neither end has enough earlier contacts to compare with.";
  }
}

/** The larger of a link's reach thresholds, which decides, with its end. */
function largerThreshold(link: LongLink): string | null {
  let larger: { node_id: number; threshold_km: number } | null = null;
  for (const profile of link.reach?.ends ?? []) {
    const threshold = profile.threshold_km;
    if (
      threshold !== null &&
      (larger === null || threshold > larger.threshold_km)
    ) {
      larger = { node_id: profile.node_id, threshold_km: threshold };
    }
  }
  return larger === null
    ? null
    : `${endName(endOf(link, larger.node_id))}'s ${formatKm(larger.threshold_km)} km`;
}

/** A link heard far apart, with each factor its flags weigh when opened. */
export function LongLinkItem({
  link,
  rules,
  latest = false,
  open,
  onToggle,
}: {
  link: LongLink;
  rules: LongLinkRules;
  /** Ranked by when last heard far apart, so say when in the summary. */
  latest?: boolean;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  const a = link.node_a;
  const b = link.node_b;
  const checks = (factor: Factor) =>
    link.checks
      .filter((check) => CHECKS[check.kind].factor === factor)
      .map((check) => (
        <li key={`${check.kind}-${check.node_id}`}>
          {checkText(check, link, rules)}
        </li>
      ));
  return (
    <li>
      <details
        className="longest-link"
        open={open}
        onToggle={(event) => {
          if (event.currentTarget.open !== open) {
            onToggle(event.currentTarget.open);
          }
        }}
      >
        <summary>
          <span className="longest-link-km">
            {formatKm(link.longest_km)} km
          </span>
          <span className="longest-link-names">
            {endName(a)} ⇄ {endName(b)}
          </span>
          {latest && link.long_heard_at !== null ? (
            <span className="muted">
              <ShortTime value={link.long_heard_at} />
            </span>
          ) : null}
          <ShownBadge shown={link.shown} rules={rules} />
          {link.doubt !== null ? (
            <DoubtBadge link={link} rules={rules} />
          ) : null}
          {link.unconfirmed ? <UnconfirmedBadge rules={rules} /> : null}
          {link.evidence.map((kind) => (
            <KindBadge key={kind.link_kind} evidence={kind} />
          ))}
          {link.checks.length > 0 ? (
            <Badge
              tone="amber"
              title={link.checks
                .map((check) => checkLabel(check, link))
                .join("\n")}
            >
              {link.checks.length}{" "}
              {link.checks.length === 1 ? "check" : "checks"}
            </Badge>
          ) : null}
        </summary>

        <div className="long-link-factors">
          <ShownFactor link={link} rules={rules} checks={checks("shown")} />
          <HeardFactor link={link} rules={rules} checks={checks("heard")} />
          <EndsFactor link={link} rules={rules} checks={checks("ends")} />
          <TerrainFactor link={link} rules={rules} checks={checks("terrain")} />
          <ReachFactor link={link} rules={rules} checks={checks("reach")} />
        </div>
      </details>
    </li>
  );
}

function FactorCard({
  title,
  verdict,
  checks,
  how,
  toCheck,
  links,
  applies = true,
  children,
}: {
  title: string;
  verdict?: ReactNode;
  checks: ReactNode[];
  how: ReactNode;
  toCheck: ReactNode;
  links?: ReactNode;
  applies?: boolean;
  children?: ReactNode;
}) {
  return (
    <section className="long-link-factor">
      <header>
        <h4>{title}</h4>
        {verdict}
      </header>
      {children}
      {checks.length > 0 ? (
        <ul className="longest-link-checks">{checks}</ul>
      ) : null}
      {applies ? (
        <dl className="long-link-help">
          <div>
            <dt>How it&apos;s decided</dt>
            <dd>{how}</dd>
          </div>
          <div>
            <dt>What to check</dt>
            <dd>
              {toCheck}
              {links === undefined ? null : (
                <span className="longest-link-actions">{links}</span>
              )}
            </dd>
          </div>
        </dl>
      ) : null}
    </section>
  );
}

interface FactorProps {
  link: LongLink;
  rules: LongLinkRules;
  checks: ReactNode[];
}

function ShownFactor({ link, rules, checks }: FactorProps) {
  const drawn = link.evidence.some((kind) => kind.map_hidden_because === null);
  const leftOutBecause = link.evidence.find(
    (kind) => kind.map_hidden_because !== null,
  )?.map_hidden_because;
  return (
    <FactorCard
      title="How pages show it"
      verdict={<ShownBadge shown={link.shown} rules={rules} />}
      checks={checks}
      how={
        <>
          <strong>Still shown long</strong>: the map draws it, or the Links page
          ranks it without a badge, within {rules.moved_km} km of its longest
          distance. <strong>Hidden</strong>: the map leaves it out and the Links
          page ranks it apart as doubtful or unconfirmed.{" "}
          <strong>Now shorter</strong>: anything else, usually because an end
          moved or its position was corrected. The map draws the ends where they
          are now and leaves out links that don&apos;t fit there; the Links page
          gives the distance when it was last heard.
        </>
      }
      toCheck={
        <>
          A link still shown long needs the factors below to back it up. A
          distance that changed while heard means an end moved: compare when it
          was longest with when each end reported its position.
        </>
      }
    >
      <dl className="long-link-facts">
        <DataItem
          label="Longest when heard"
          value={
            <>
              {formatKm(link.longest_km)} km,{" "}
              <ShortTime value={link.longest_at} />
            </>
          }
        />
        {link.long_heard_at !== null &&
        link.long_heard_at !== link.longest_at ? (
          <DataItem
            label={`Last heard ${rules.long_link_km} km+ apart`}
            value={<ShortTime value={link.long_heard_at} />}
          />
        ) : null}
        <DataItem
          label="Shortest when heard"
          value={`${formatKm(link.shortest_km)} km`}
        />
        <DataItem
          label="Links page (all history)"
          value={
            <>
              {link.latest_km === null
                ? "no distance when last heard"
                : `${formatKm(link.latest_km)} km, as last heard`}
              {link.doubt !== null || link.unconfirmed ? (
                <span className="muted"> · ranked apart</span>
              ) : null}
            </>
          }
        />
        <DataItem
          label="Map (all history)"
          value={
            drawn
              ? `drawn ${formatKm(link.drawn_km)} km apart`
              : `left out: ${leftOutBecause}`
          }
        />
        <DataItem
          label="Reported positions now"
          value={
            link.now_km === null
              ? "an end has none"
              : `${formatKm(link.now_km)} km apart`
          }
        />
        <DataItem label="Channels" value={link.channels.join(", ")} />
      </dl>
    </FactorCard>
  );
}

function HeardFactor({ link, rules, checks }: FactorProps) {
  const a = link.node_a;
  const b = link.node_b;
  const direct = link.evidence.find((kind) => kind.link_kind === "direct");
  const traceroutes = link.evidence.some(
    (kind) => kind.link_kind === "traceroute",
  );
  const packets = [
    direct !== undefined && direct.a_to_b > 0 ? (
      <DirectPacketsLink key="ab" from={a} to={b} />
    ) : null,
    direct !== undefined && direct.b_to_a > 0 ? (
      <DirectPacketsLink key="ba" from={b} to={a} />
    ) : null,
    traceroutes ? (
      <a
        key="traceroutes"
        className="text-button"
        href={tracerouteHopHref(a.node.node_id, b.node.node_id)}
      >
        Traceroutes with this hop
      </a>
    ) : null,
  ];
  return (
    <FactorCard
      title="What heard it"
      verdict={
        link.doubt?.reason === "signal" ? (
          <DoubtBadge link={link} rules={rules} />
        ) : null
      }
      checks={checks}
      how={
        <>
          The table counts every report in saved history, each way. More hours,
          both directions and more than one kind make a real link likely; one
          packet, or traceroutes alone, can be an MQTT bridge or a wrong
          position. Route steps of exactly 0 dB are hops over MQTT.{" "}
          <strong>Signal mismatch</strong>: its newest hour was heard directly{" "}
          {rules.signal_min_km} km or more apart with a signal so strong that,
          over that distance in free space, it would need over{" "}
          {rules.signal_power_dbm} dBm ({watts(rules.signal_power_dbm)}) of
          transmit power: the ends are probably nearer than their positions say.
        </>
      }
      toCheck={
        <>
          {direct !== undefined
            ? "Open the packets heard directly: do the gateway, hops and signal fit a link this long? "
            : null}
          {traceroutes
            ? "In traceroutes, do the hops either side of this one fit where the nodes are? "
            : null}
          {link.evidence.some((kind) => kind.link_kind === "neighbor")
            ? "A neighbour report has no packet to open here: on the reporting end's page, do its other neighbours fit where it is?"
            : null}
        </>
      }
      links={packets.some((item) => item !== null) ? packets : undefined}
    >
      <WideTable>
        <table>
          <thead>
            <tr>
              <th scope="col">Kind</th>
              <th scope="col" title={`Times ${endName(b)} heard ${endName(a)}`}>
                {endName(a)} → {endName(b)}
              </th>
              <th scope="col" title={`Times ${endName(a)} heard ${endName(b)}`}>
                {endName(b)} → {endName(a)}
              </th>
              <th scope="col" title="Distinct hours it was heard in">
                Hours
              </th>
              <th
                scope="col"
                title="Signal-to-noise ratio over every report with one"
              >
                Average SNR
              </th>
              <th scope="col" title="Start of the first hour it was heard in">
                First hour
              </th>
              <th scope="col">Last heard</th>
              <th scope="col" title="Its longest distance when heard">
                Longest km
              </th>
              <th scope="col" title="Badges on its Links page rows">
                Links page
              </th>
              <th
                scope="col"
                title="Whether the map, showing all history, draws this kind of link"
              >
                Map
              </th>
            </tr>
          </thead>
          <tbody>
            {link.evidence.map((kind) => (
              <tr key={kind.link_kind}>
                <th scope="row">{KINDS[kind.link_kind].label}</th>
                <td>{kind.a_to_b}</td>
                <td>{kind.b_to_a}</td>
                <td>{kind.hours}</td>
                <td>
                  {kind.average_snr === null
                    ? "—"
                    : `${kind.average_snr.toFixed(1)} dB`}
                  {kind.zero_snr ? (
                    <span className="muted"> (all 0 dB)</span>
                  ) : null}
                </td>
                <td>
                  <ShortTime value={kind.first_hour_at} />
                </td>
                <td>
                  <ShortTime value={kind.last_seen_at} />
                </td>
                <td>{formatKm(kind.longest_km)}</td>
                <td>
                  <LinksPageBadges link={link} evidence={kind} rules={rules} />
                </td>
                <td>
                  {kind.map_hidden_because === null
                    ? "drawn"
                    : `left out: ${kind.map_hidden_because}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </WideTable>
    </FactorCard>
  );
}

function EndsFactor({ link, rules, checks }: FactorProps) {
  const ends = [link.node_a, link.node_b];
  const reason = link.doubt?.reason;
  const anyBlamed = ends.some((end) => end.blamed_partners > 0);
  const row = (label: ReactNode, cell: (end: LongLinkEnd) => ReactNode) => (
    <tr>
      {label}
      {ends.map((end) => (
        <td key={end.node.node_id}>{cell(end)}</td>
      ))}
    </tr>
  );
  return (
    <FactorCard
      title="Positions of its ends"
      verdict={
        <>
          {reason === "shared" || reason === "position" ? (
            <DoubtBadge link={link} rules={rules} />
          ) : null}
          {link.unconfirmed ? <UnconfirmedBadge rules={rules} /> : null}
        </>
      }
      checks={checks}
      how={
        <>
          <strong>Doubtful position</strong>: the median distance to the placed
          nodes it heard, or that heard it, directly over the last{" "}
          {rules.partner_days} days is over its limit:{" "}
          {rules.doubtful_partner_km} km, or its radio horizon when it reports a
          high altitude. A far partner whose own other partners place it
          elsewhere is not counted, and measured traceroute hops near its
          position can clear it. <strong>Shared node number</strong>: two radios
          with this number were on air at once, from different places.{" "}
          <strong>Unconfirmed</strong>: the Links page ranks distances of{" "}
          {rules.unconfirmed_km} km or more apart unless both ends have reported
          a position since it was heard and neither has moved since.
        </>
      }
      toCheck={
        <>
          Open each end: are its direct partners near where it says it is? Is
          its position its own recent report, not a rounded map report or an
          estimate? If it moved, was this heard before or after?
        </>
      }
      links={
        ends.some((end) => end.shared) ? (
          <a className="text-button" href="#diagnostics/devices">
            ID conflicts
          </a>
        ) : undefined
      }
    >
      <WideTable>
        <table className="long-link-ends">
          <thead>
            <tr>
              <th scope="col">
                <span className="visually-hidden">Fact</span>
              </th>
              {ends.map((end) => (
                <th scope="col" key={end.node.node_id}>
                  <NodeLink
                    nodeId={end.node.node_id}
                    name={nodeName(end.node)}
                  />{" "}
                  {end.doubtful ? (
                    <Badge tone="amber" title={partnersTitle(end)}>
                      doubtful position
                    </Badge>
                  ) : null}{" "}
                  {end.shared ? (
                    <Badge
                      tone="amber"
                      title="Two radios with this node number were on air at once"
                    >
                      shared node number
                    </Badge>
                  ) : null}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {row(<th scope="row">Position</th>, (end) => (
              <EndPosition end={end} />
            ))}
            {row(
              <th scope="row" title="0 m is how apps say it is not set">
                Altitude
              </th>,
              (end) =>
                end.altitude_meters === null
                  ? "—"
                  : end.altitude_meters === 0
                    ? "0 m (not set)"
                    : `${end.altitude_meters.toLocaleString()} m`,
            )}
            {row(
              <th
                scope="row"
                title={`When it last reported a position over ${rules.moved_km} km from its previous one`}
              >
                Last moved
              </th>,
              (end) =>
                end.moved_at === null ? (
                  "not seen moving"
                ) : (
                  <ShortTime value={end.moved_at} />
                ),
            )}
            {row(
              <th
                scope="row"
                title="Placed nodes it heard, or that heard it, with no relay, each at its shortest distance"
              >
                Direct partners, {rules.partner_days} days
              </th>,
              (end) =>
                end.direct_partners === 0
                  ? "none placed"
                  : `${end.direct_partners}: nearest ${formatKm(end.nearest_partner_km)} km, median ${formatKm(end.median_partner_km)} km`,
            )}
            {row(
              <th
                scope="row"
                title="Its partners' median distance against its limit"
              >
                Position check
              </th>,
              (end) => (
                <PositionVerdict end={end} />
              ),
            )}
            {anyBlamed
              ? row(
                  <th
                    scope="row"
                    title="Far partners left out because their own other partners place them elsewhere"
                  >
                    Partners not counted
                  </th>,
                  (end) => end.blamed_partners,
                )
              : null}
            {row(
              <th scope="row">
                <span className="visually-hidden">Long links</span>
              </th>,
              (end) => (
                <a
                  className="text-button"
                  href={diagnosticsLinksHref(end.node.node_id)}
                >
                  Its long links
                </a>
              ),
            )}
          </tbody>
        </table>
      </WideTable>
    </FactorCard>
  );
}

function EndPosition({ end }: { end: LongLinkEnd }) {
  const node = end.node;
  if (node.latitude === null || node.longitude === null) return <>none</>;
  return (
    <>
      {node.latitude.toFixed(4)}, {node.longitude.toFixed(4)}
      <div className="muted">
        {node.estimated ? (
          "estimated from its neighbours"
        ) : end.position_observed_at === null ? null : (
          <>
            {end.position_from_map_report ? "map report" : "reported"}{" "}
            <ShortTime value={end.position_observed_at} />
          </>
        )}
      </div>
    </>
  );
}

/** Whether an end's direct partners fit its reported position. */
function PositionVerdict({ end }: { end: LongLinkEnd }) {
  if (end.direct_partners === 0 || end.median_partner_km === null) {
    return end.doubtful ? (
      <span className="long-link-fail">doubtful</span>
    ) : (
      <span className="muted">no partners to judge by</span>
    );
  }
  const numbers = `median ${formatKm(end.median_partner_km)} km, limit ${formatKm(end.doubt_limit_km)} km`;
  if (end.doubtful) {
    return <span className="long-link-fail">doubtful: {numbers}</span>;
  }
  if (end.over_limit && end.traceroute_corroborated) {
    return <>cleared by nearby traceroute hops: {numbers}</>;
  }
  return end.over_limit ? (
    <span className="long-link-fail">over its limit: {numbers}</span>
  ) : (
    <span className="long-link-pass">fits: {numbers}</span>
  );
}

function partnersTitle(end: LongLinkEnd): string {
  return end.median_partner_km === null
    ? `${endName(end)}'s direct partners are too far from its reported position`
    : `${endName(end)}'s direct partners are a median ${formatKm(end.median_partner_km)} km away, over its ${formatKm(end.doubt_limit_km)} km limit`;
}

function TerrainFactor({ link, rules, checks }: FactorProps) {
  const terrain = link.terrain;
  const a = link.node_a.node.node_id;
  const b = link.node_b.node.node_id;
  const suspects =
    link.doubt?.reason === "terrain"
      ? link.doubt.suspects.map((nodeId) => endOf(link, nodeId))
      : [];
  const [label, tone]: [string, Tone] =
    terrain === null
      ? link.longest_km >= rules.long_link_km
        ? ["not checked yet", "neutral"]
        : ["not needed", "neutral"]
      : terrain.confirmed
        ? ["passes: ends confirmed in place", "green"]
        : terrain.unlikely
          ? ["unlikely", "amber"]
          : ["possible", "green"];
  return (
    <FactorCard
      title="Terrain"
      applies={link.longest_km >= rules.long_link_km}
      verdict={
        <>
          <Badge tone={tone}>{label}</Badge>
          {suspects.length > 0 ? (
            <Badge
              tone="amber"
              title="It is in more links the terrain check finds unlikely"
            >
              probably {suspects.map(endName).join(" and ")}
            </Badge>
          ) : null}
        </>
      }
      checks={checks}
      how={
        <>
          Checked from {rules.long_link_km} km, at the ends&apos; positions when
          it was longest. Ground radio reaches that far only from high up or
          over sea, so it is unlikely unless the higher end stands{" "}
          {rules.terrain_above_meters} m or more above the median ground between
          (sea counts as 0 m), or {Math.round(rules.terrain_sea_share * 100)}%
          or more of the path is sea. Over {rules.terrain_max_land_km} km of
          land is unlikely whatever the heights. A pair whose ends are both
          confirmed in place, each heard directly within{" "}
          {rules.confirming_partner_km} km of a partner and, when checked, never
          moved or doubted, passes. When it is doubted, the end in more unlikely
          links is the likelier culprit.
        </>
      }
      toCheck={
        <>
          Does the line of sight clear the ground? Is the altitude believable? A
          wrong position usually makes several unlikely links, so look at the
          suspect&apos;s other long links.
        </>
      }
      links={
        <a
          className="text-button"
          href={lineOfSightHref(a, b, link.longest_at)}
        >
          Line of sight when longest
        </a>
      }
    >
      {terrain === null ? (
        link.longest_km >= rules.long_link_km ? null : (
          <p className="muted">
            Only links heard {rules.long_link_km} km or more apart are checked.
          </p>
        )
      ) : (
        <TerrainTests link={link} rules={rules} />
      )}
    </FactorCard>
  );
}

function TerrainTests({
  link,
  rules,
}: {
  link: LongLink;
  rules: LongLinkRules;
}) {
  const terrain = link.terrain!;
  const land = link.longest_km * (1 - terrain.sea_share);
  const tests: [string, string, string, boolean | null][] = [
    [
      "Higher end above the ground between",
      `${Math.round(terrain.above_meters).toLocaleString()} m`,
      `${rules.terrain_above_meters} m or more, or the sea test`,
      terrain.above_meters >= rules.terrain_above_meters,
    ],
    [
      "Path over sea",
      `${Math.round(terrain.sea_share * 100)}%`,
      `${Math.round(rules.terrain_sea_share * 100)}% or more, or the height test`,
      terrain.sea_share >= rules.terrain_sea_share,
    ],
    [
      "Land crossed, about",
      `${formatKm(land)} km`,
      `${rules.terrain_max_land_km} km or less`,
      land <= rules.terrain_max_land_km,
    ],
    [
      "Both ends confirmed in place",
      terrain.confirmed ? "yes" : "no",
      "yes passes whatever the rest",
      terrain.confirmed ? true : null,
    ],
  ];
  return (
    <WideTable>
      <table>
        <thead>
          <tr>
            <th scope="col">Test</th>
            <th scope="col">This link</th>
            <th scope="col">Passes with</th>
            <th scope="col">Result</th>
          </tr>
        </thead>
        <tbody>
          {tests.map(([test, value, passes, ok]) => (
            <tr key={test}>
              <th scope="row">{test}</th>
              <td>{value}</td>
              <td>{passes}</td>
              <td>
                {ok === null ? (
                  <span className="muted">no override</span>
                ) : ok ? (
                  <span className="long-link-pass">passes</span>
                ) : (
                  <span className="long-link-fail">fails</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </WideTable>
  );
}

function ReachFactor({ link, rules, checks }: FactorProps) {
  const reach = link.reach;
  const kinds = new Set(link.checks.map((check) => check.kind));
  const [label, tone]: [string, Tone] =
    reach === null
      ? ["not compared", "neutral"]
      : kinds.has("reach_long")
        ? ["unusually long", "amber"]
        : kinds.has("reach_unknown")
          ? ["not enough history", "neutral"]
          : ["within reach", "green"];
  return (
    <FactorCard
      title="Reach compared with earlier contacts"
      applies={reach !== null}
      verdict={<Badge tone={tone}>{label}</Badge>}
      checks={checks}
      how={
        <>
          Its longest distance is compared with each end&apos;s earlier contacts
          with other partners: the same kind of link on the same channel, since
          the end last moved. An end&apos;s review threshold is{" "}
          {rules.reach_review_km} km + {rules.reach_factor} × (median + spread)
          of those partners&apos; distances, once it has {rules.reach_partners}{" "}
          partners heard in {rules.reach_hours} hours over {rules.reach_days}{" "}
          days. It is flagged when longer than the larger threshold. Only links
          of {rules.reach_review_km} km or more are compared.
        </>
      }
      toCheck={
        <>
          A node that never reached this far before is suspect. A large median
          can mean that end is misplaced itself, which lifts its threshold.
          Unusual propagation lifts many links at the same time: order the list
          by Recent first and look for other long links heard then.
        </>
      }
    >
      {reach === null ? (
        <p className="muted">
          Only links heard {rules.reach_review_km} km or more apart are
          compared.
        </p>
      ) : (
        <ReachTable link={link} reach={reach} />
      )}
    </FactorCard>
  );
}

function ReachTable({
  link,
  reach,
}: {
  link: LongLink;
  reach: NonNullable<LongLink["reach"]>;
}) {
  const ends = reach.ends;
  const larger = largerThreshold(link);
  const row = (
    label: ReactNode,
    cell: (profile: (typeof ends)[number]) => ReactNode,
  ) => (
    <tr>
      {label}
      {ends.map((profile) => (
        <td key={profile.node_id}>{cell(profile)}</td>
      ))}
    </tr>
  );
  return (
    <>
      <p className="long-link-context">
        {KINDS[reach.link_kind].label} on {reach.channel}, before{" "}
        <ShortTime value={reach.before_at} />
      </p>
      <WideTable>
        <table className="long-link-ends">
          <thead>
            <tr>
              <th scope="col">
                <span className="visually-hidden">Measure</span>
              </th>
              {ends.map((profile) => (
                <th scope="col" key={profile.node_id}>
                  <NodeLink
                    nodeId={profile.node_id}
                    name={nodeName(endOf(link, profile.node_id).node)}
                  />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {row(
              <th
                scope="row"
                title="The nodes it was linked with before, the other end left out"
              >
                Other partners
              </th>,
              (profile) => profile.partners,
            )}
            {row(
              <th scope="row" title="Distinct hours and UTC dates heard in">
                Heard in
              </th>,
              (profile) =>
                `${profile.hours} ${profile.hours === 1 ? "hour" : "hours"}, ${profile.days} ${profile.days === 1 ? "day" : "days"}`,
            )}
            {row(
              <th
                scope="row"
                title="Middle of the partners' distances, each partner's own median"
              >
                Median distance
              </th>,
              (profile) =>
                profile.median_km === null
                  ? "—"
                  : `${formatKm(profile.median_km)} km`,
            )}
            {row(
              <th
                scope="row"
                title="Width of the middle half of the partners' distances"
              >
                Spread (IQR)
              </th>,
              (profile) =>
                profile.iqr_km === null
                  ? "—"
                  : `${formatKm(profile.iqr_km)} km`,
            )}
            {row(<th scope="row">Review threshold</th>, (profile) =>
              profile.threshold_km === null ? (
                <span className="muted">not enough history</span>
              ) : profile.threshold_km < link.longest_km ? (
                <span className="long-link-fail">
                  {formatKm(profile.threshold_km)} km
                </span>
              ) : (
                <span className="long-link-pass">
                  {formatKm(profile.threshold_km)} km
                </span>
              ),
            )}
            {row(
              <th
                scope="row"
                title="From the first hour heard to the last contact"
              >
                Earlier contacts
              </th>,
              (profile) =>
                profile.first_hour_at === null ||
                profile.last_seen_at === null ? (
                  "—"
                ) : (
                  <>
                    <ShortTime value={profile.first_hour_at} /> –{" "}
                    <ShortTime value={profile.last_seen_at} />
                  </>
                ),
            )}
          </tbody>
        </table>
      </WideTable>
      {larger !== null &&
      !link.checks.some((check) => check.kind === "reach_long") ? (
        <p className="long-link-context long-link-result">
          Its longest distance, {formatKm(link.longest_km)} km, is within the
          larger review threshold, {larger}.
        </p>
      ) : null}
    </>
  );
}

function KindBadge({ evidence }: { evidence: Evidence }) {
  const kind = KINDS[evidence.link_kind];
  return (
    <Badge
      title={
        evidence.zero_snr
          ? `${kind.title}. Every step at exactly 0 dB: a hop over MQTT`
          : kind.title
      }
    >
      {kind.tag} {evidence.a_to_b + evidence.b_to_a}
      {evidence.zero_snr ? " · 0 dB" : ""}
    </Badge>
  );
}

/** The badges the Links page gives one kind's rows of a long link. */
function LinksPageBadges({
  link,
  evidence,
  rules,
}: {
  link: LongLink;
  evidence: Evidence;
  rules: LongLinkRules;
}) {
  const doubts = evidence.doubts;
  if (doubts.length === 0) return <span className="muted">none</span>;
  return (
    <>
      {doubts.includes("shared") ? <SharedBadge /> : null}
      <DoubtfulBadge
        suspects={
          doubts.includes("doubtful")
            ? [link.node_a, link.node_b]
                .filter((end) => end.doubtful)
                .map(endName)
            : []
        }
        unlikely={doubts.includes("terrain")}
        signal={doubts.includes("signal")}
      />
      {doubts.includes("unconfirmed") ? (
        <UnconfirmedBadge rules={rules} />
      ) : null}
    </>
  );
}

function UnconfirmedBadge({ rules }: { rules: LongLinkRules }) {
  return (
    <Badge
      tone="neutral"
      title={`The Links page ranks distances of ${rules.unconfirmed_km} km or more apart unless both ends reported a position since and neither moved`}
    >
      unconfirmed
    </Badge>
  );
}

/**
 * Why the Links page ranks a long link apart (LongLink.doubt), naming the
 * end to blame.
 */
function DoubtBadge({ link, rules }: { link: LongLink; rules: LongLinkRules }) {
  const doubt = link.doubt!;
  const suspects = doubt.suspects.map((nodeId) => endOf(link, nodeId));
  const named = suspects.map(endName).join(" and ");
  if (doubt.reason === "shared") {
    return (
      <Badge
        tone="amber"
        title="Two radios with this node number were on air at once, so its distances mix places"
      >
        shared node number: {named}
      </Badge>
    );
  }
  if (doubt.reason === "signal") {
    return (
      <Badge
        tone="amber"
        title={`Heard directly with a signal far too strong for the distance: it would need over ${rules.signal_power_dbm} dBm of transmit power`}
      >
        signal mismatch
      </Badge>
    );
  }
  if (doubt.reason === "position") {
    return (
      <Badge tone="amber" title={suspects.map(partnersTitle).join("\n")}>
        doubtful position: {named}
      </Badge>
    );
  }
  return (
    <Badge
      tone="amber"
      title={`Heard ${rules.long_link_km} km+ apart with no high end or sea path between, so an end's position is probably wrong${suspects.length > 0 ? `. ${named} is in more unlikely links` : ""}`}
    >
      doubtful by terrain
      {suspects.length > 0 ? `: probably ${named}` : ""}
    </Badge>
  );
}

/** Packets `from` sent that `to` heard with no relay, in the packet list. */
function DirectPacketsLink({
  from,
  to,
}: {
  from: LongLinkEnd;
  to: LongLinkEnd;
}) {
  return (
    <a
      className="text-button"
      href={diagnosticsPacketsHref(from.node.node_id, to.node.node_id)}
      title={`Packets ${endName(from)} sent that ${endName(to)} heard with no relay`}
    >
      Packets {endName(from)} → {endName(to)}
    </a>
  );
}

/** A transmit power in dBm as watts, to show how far off a figure is. */
function watts(dbm: number): string {
  const value = 10 ** ((dbm - 30) / 10);
  return value >= 1000
    ? `${(value / 1000).toLocaleString()} kW`
    : `${value.toLocaleString()} W`;
}
