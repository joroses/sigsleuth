import { Suspense, lazy, useState } from "react";

import {
  type HeardHour,
  type HeardPositionList,
  type HopObservations,
  type LinkHours,
  type LinkKind,
  type LinkRow,
  type NodeSummary,
  linkHoursUrl,
  linkPositionsUrl,
  tracerouteHopsUrl,
} from "./api";
import { now as clockNow } from "./clock";
import { HopCostLine, HopObservationList } from "./HopObservations";
import { type LatLon } from "./lineOfSight";
import { linkTier, linkTierColor } from "./linkQuality";
import { formatNodeId, nodeName, tracerouteHopHref } from "./nodeDisplay";
import { Badge, Icon, LinkArrow, ShortTime } from "./ui";
import { useApi } from "./useApi";

// The map brings in MapLibre, so it loads when a row is first opened.
const LinkMinimap = lazy(() =>
  import("./LinkMinimap").then((module) => ({ default: module.LinkMinimap })),
);

const HOUR_MS = 3_600_000;

/** What a link's details need: a Links row or a Zones pair link. */
export type DetailedLink = Pick<
  LinkRow,
  | "node_a"
  | "node_b"
  | "last_seen_at"
  | "channel"
  | "spreading_factor"
  | "a_to_b"
  | "b_to_a"
>;

/**
 * A Links or Zones row opened: the pair on a map where they were when last heard, and
 * for each direction the hours it was heard in the window, then, for
 * traceroute links, its newest packets.
 */
export function LinkDetails({
  link,
  linkKind,
  nodes,
  since,
}: {
  link: DetailedLink;
  linkKind: LinkKind;
  nodes: ReadonlyMap<number, NodeSummary>;
  since: string | undefined;
}) {
  const traceroutes = linkKind === "traceroute";
  const heard = useApi<HeardPositionList>(
    linkPositionsUrl(link.node_a, link.node_b, Date.parse(link.last_seen_at)),
  );
  const hours = useApi<LinkHours>(
    linkHoursUrl(linkKind, link.node_a, link.node_b, link.channel, since),
  );
  const hops = useApi<HopObservations>(
    traceroutes
      ? tracerouteHopsUrl(link.node_a, link.node_b, since, link.channel)
      : null,
  );
  // The strips end at the hour the row was opened.
  const [openedAt] = useState(clockNow);
  const name = (nodeId: number) =>
    nodeName(nodes.get(nodeId)) ?? formatNodeId(nodeId);
  const directions = [
    { direction: "a_to_b", from: link.node_a, to: link.node_b },
    { direction: "b_to_a", from: link.node_b, to: link.node_a },
  ] as const;

  const end = openedAt - (openedAt % HOUR_MS);
  const heardHours = [
    ...(hours.data?.a_to_b ?? []),
    ...(hours.data?.b_to_a ?? []),
  ].map((hour) => Date.parse(hour.hour_start));
  const sinceMs = since === undefined ? null : Date.parse(since);
  const start =
    sinceMs !== null
      ? sinceMs - (sinceMs % HOUR_MS)
      : Math.min(end, ...heardHours);

  return (
    <div className="link-details">
      <div className="link-details-map">
        <PairMap
          link={link}
          linkKind={linkKind}
          nodes={nodes}
          heard={heard}
          name={name}
        />
      </div>
      <div className="link-details-side">
        {directions.map(({ direction, from, to }) => (
          <section key={direction}>
            <p className="section-label">
              {name(from)} <LinkArrow /> {name(to)}
            </p>
            {hours.error !== null ? (
              <p className="muted">{hours.error}</p>
            ) : hours.data === null ? (
              <p className="muted">Loading…</p>
            ) : (
              <HourStrip
                hours={hours.data[direction]}
                start={start}
                end={end}
                spreadingFactor={link.spreading_factor}
              />
            )}
            {hours.data?.[direction].some((hour) => hour.suspect_distance) ? (
              <Badge tone="amber" title="Signal suggests a suspect distance">
                suspect distance
              </Badge>
            ) : null}
            {traceroutes && hops.data !== null ? (
              <p className="hop-cost">
                <HopCostLine
                  hops={hops}
                  direction={direction}
                  name={name}
                  unknown="Hop cost unknown"
                />
              </p>
            ) : null}
            {traceroutes ? (
              <HopObservationList
                hops={hops}
                direction={direction}
                fromNodeId={from}
                toNodeId={to}
                spreadingFactor={link.spreading_factor}
              />
            ) : null}
          </section>
        ))}
        {traceroutes ? (
          <a
            className="text-button hop-lists-all"
            href={tracerouteHopHref(link.node_a, link.node_b)}
          >
            <Icon name="route" />
            All traceroutes with this hop
          </a>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Where A and B were when the link was last heard, by their own positions;
 * a node with none up to then is drawn where the page places it now.
 */
function PairMap({
  link,
  linkKind,
  nodes,
  heard,
  name,
}: {
  link: DetailedLink;
  linkKind: LinkKind;
  nodes: ReadonlyMap<number, NodeSummary>;
  heard: { data: HeardPositionList | null; error: string | null };
  name: (nodeId: number) => string;
}) {
  if (heard.data === null && heard.error === null) {
    return <p className="muted">Loading map…</p>;
  }
  const notes: string[] = [];
  const place = (nodeId: number): LatLon | null => {
    const then = heard.data?.items.find((item) => item.node_id === nodeId);
    if (then !== undefined) return then;
    const now = nodes.get(nodeId);
    if (now?.latitude == null || now.longitude == null) {
      notes.push(`${name(nodeId)} has no known position.`);
      return null;
    }
    notes.push(
      `${name(nodeId)} is drawn where it is ${now.estimated ? "estimated to be " : ""}now: no position of its own up to then.`,
    );
    return { latitude: now.latitude, longitude: now.longitude };
  };
  const a = place(link.node_a);
  const b = place(link.node_b);
  return (
    <>
      {a !== null && b !== null ? (
        <Suspense fallback={<p className="muted">Loading map…</p>}>
          <LinkMinimap linkKind={linkKind} link={link} a={a} b={b} />
        </Suspense>
      ) : null}
      {notes.length > 0 ? <p className="muted">{notes.join(" ")}</p> : null}
    </>
  );
}

/**
 * One direction's hours from start to end (hour starts in ms), each heard
 * hour in the tier of its average SNR; an hour with no SNR is grey, and an
 * hour not heard is left blank.
 */
function HourStrip({
  hours,
  start,
  end,
  spreadingFactor,
}: {
  hours: readonly HeardHour[];
  start: number;
  end: number;
  spreadingFactor: number;
}) {
  const count = Math.max(1, (end - start) / HOUR_MS + 1);
  return (
    <figure className="hour-strip">
      <svg
        viewBox={`0 0 ${count} 1`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Heard in ${hours.length} of ${count} hours`}
      >
        <rect className="hour-strip-track" width={count} height={1} />
        {hours.map((hour) => {
          const at = Date.parse(hour.hour_start);
          const snr =
            hour.average_snr === null
              ? "no SNR"
              : `${hour.average_snr.toFixed(1)} dB`;
          return (
            <rect
              key={hour.hour_start}
              x={(at - start) / HOUR_MS}
              width={1}
              height={1}
              fill={linkTierColor(linkTier(hour.average_snr, spreadingFactor))}
              stroke={hour.suspect_distance ? "#d97706" : undefined}
              strokeWidth={0.15}
            >
              <title>
                {`${new Date(at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}: ${hour.observations} heard, ${snr}`}
              </title>
            </rect>
          );
        })}
      </svg>
      <figcaption className="hour-strip-axis muted">
        <ShortTime value={new Date(start).toISOString()} />
        <span>
          heard in {hours.length} of {count} {count === 1 ? "hour" : "hours"}
        </span>
        <ShortTime value={new Date(end).toISOString()} />
      </figcaption>
    </figure>
  );
}
