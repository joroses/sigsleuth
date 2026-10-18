// The columns the Links page and a node page's links share.

import type { LinkRow, NodeSummary } from "./api";
import { type LinkSort, asymmetry, touches, weakerMargin } from "./linkRows";
import {
  doubtedNames,
  formatKm,
  lineOfSightHref,
  tracerouteHopHref,
} from "./nodeDisplay";
import {
  AirborneBadge,
  BothWays,
  DirectionSnr,
  DoubtfulBadge,
  Icon,
  MarginValue,
  PendingBadge,
  ShortTime,
  SortHeading,
} from "./ui";

/**
 * The shared columns' headings, from Distance to Last seen. `ways` names the
 * two figures around ⇄.
 */
export function LinkHeadings({
  sort,
  ascending,
  onSort,
  ways,
  waysTitle,
}: {
  sort: LinkSort;
  ascending: boolean;
  onSort: (sort: LinkSort) => void;
  ways: string;
  waysTitle: string;
}) {
  const heading = { active: sort, descending: !ascending, onSort };
  return (
    <>
      <SortHeading label="Distance" note="km" sort="distance" {...heading} />
      <th scope="col">Channel</th>
      <SortHeading
        label="Seen"
        note="times"
        title={`Observations each way: ${waysTitle}`}
        sort="observations"
        {...heading}
      />
      <th scope="col" title={`Average SNR each way: ${waysTitle}`}>
        SNR<span className="heading-note">avg dB, {ways}</span>
      </th>
      <SortHeading
        label="Margin"
        note="lowest"
        title="Lowest of both directions: dB above the weakest signal the channel can still decode"
        sort="weaker_margin"
        {...heading}
      />
      <SortHeading label="Asym." note="dB" sort="asymmetry" {...heading} />
      <SortHeading label="Last seen" sort="last_seen" {...heading} />
    </>
  );
}

/** The cells under LinkHeadings, with node_a's figures left of ⇄. */
export function LinkCells({
  link,
  nodes,
  doubtful,
  airborne,
}: {
  link: LinkRow;
  nodes: ReadonlyMap<number, NodeSummary>;
  doubtful: ReadonlySet<number>;
  airborne: ReadonlySet<number>;
}) {
  const margin = weakerMargin(link);
  return (
    <>
      <td className="number">
        {formatKm(link.distance_km)}
        <DoubtfulBadge
          suspects={
            link.distance_km === null
              ? []
              : doubtedNames([link.node_a, link.node_b], doubtful, nodes)
          }
          unlikely={link.unlikely}
        />
        {link.distance_km !== null && touches(link, airborne) ? (
          <AirborneBadge />
        ) : null}
        {link.pending ? <PendingBadge /> : null}
        {link.distance_km !== null ? (
          <a
            className="los-link"
            href={lineOfSightHref(link.node_a, link.node_b, link.last_seen_at)}
            title="Line of sight where they were when last heard"
            aria-label="Line of sight"
          >
            <Icon name="profile" />
          </a>
        ) : null}
      </td>
      <td>{link.channel}</td>
      <td className="number">
        <BothWays
          left={link.a_to_b.observations}
          right={link.b_to_a.observations}
        />
      </td>
      <td className="number">
        <BothWays
          left={
            <DirectionSnr
              direction={link.a_to_b}
              spreadingFactor={link.spreading_factor}
            />
          }
          right={
            <DirectionSnr
              direction={link.b_to_a}
              spreadingFactor={link.spreading_factor}
            />
          }
        />
      </td>
      <td className="number">
        {margin === null ? "—" : <MarginValue value={margin} />}
      </td>
      <td className="number">{formatDb(asymmetry(link))}</td>
      <td>
        <ShortTime value={link.last_seen_at} />
      </td>
    </>
  );
}

/** The Explorer column's cell: a traceroute link's runs with this hop. */
export function ExplorerCell({ link }: { link: LinkRow }) {
  return (
    <td>
      {link.link_kind === "traceroute" ? (
        <a
          className="icon-button"
          href={tracerouteHopHref(link.node_a, link.node_b)}
          title="All traceroutes with this hop"
          aria-label="All traceroutes with this hop"
        >
          <Icon name="route" />
        </a>
      ) : null}
    </td>
  );
}

export function MoreRows({
  shown,
  total,
  onMore,
}: {
  shown: number;
  total: number;
  onMore: () => void;
}) {
  return (
    <p className="end-note">
      Showing {Math.min(shown, total).toLocaleString()} of{" "}
      {total.toLocaleString()}.{" "}
      {shown < total ? (
        <button type="button" className="secondary-button" onClick={onMore}>
          Show more
        </button>
      ) : null}
    </p>
  );
}

function formatDb(value: number | null): string {
  return value === null ? "—" : value.toFixed(1);
}
