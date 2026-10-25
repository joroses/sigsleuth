import type { ReactNode } from "react";

import {
  type LinkDirection,
  type LinkKind,
  type MapLink,
  type HopObservations,
  tracerouteHopsUrl,
} from "./api";
import { HopCostTag, HopObservationList } from "./HopObservations";
import {
  LINK_TIERS,
  type LinkTier,
  fadeMargin,
  linkTier,
  linkTierColor,
  linkTierLabel,
} from "./linkQuality";
import type { LinkTierChoice } from "./mapLinkLayers";
import {
  formatKm,
  lineOfSightHref,
  linkDetailsHref,
  nodeHref,
  nodeLabel,
  shortLabel,
  tracerouteHopHref,
} from "./nodeDisplay";
import { Icon, IconButton, ShortTime } from "./ui";
import { useApi } from "./useApi";

/** The names MapLinkDetails shows for a node. */
export interface LinkEnd {
  node_id: number;
  long_name: string | null;
  short_name: string | null;
}

/**
 * A selected link in the map sidebar or beside the Zones map: both nodes,
 * a card with each direction's signal at a glance, a traceroute link's
 * latest packets, and links to its other pages, each named where it applies.
 * Zones omits what its table row already shows (linkSection false).
 */
export function MapLinkDetails({
  linkKind,
  link,
  nodeA,
  nodeB,
  km,
  since,
  onClose,
  linkSection = true,
}: {
  linkKind: LinkKind;
  link: MapLink;
  nodeA: LinkEnd;
  nodeB: LinkEnd;
  km: number | null;
  since: string | undefined;
  onClose: () => void;
  linkSection?: boolean;
}) {
  const hops = useApi<HopObservations>(
    linkKind === "traceroute"
      ? tracerouteHopsUrl(nodeA.node_id, nodeB.node_id, since, link.channel)
      : null,
  );
  const name = (nodeId: number) =>
    nodeLabel(nodeId === nodeA.node_id ? nodeA : nodeB);
  const way = (
    from: LinkEnd,
    to: LinkEnd,
    direction: LinkDirection,
    hopDirection: "a_to_b" | "b_to_a",
  ) => (
    <CardWay
      linkKind={linkKind}
      from={from}
      to={to}
      direction={direction}
      spreadingFactor={link.spreading_factor}
      hopCost={
        linkKind === "traceroute" ? (
          <HopCostTag hops={hops} direction={hopDirection} name={name} />
        ) : null
      }
    />
  );
  // Above the card: the channel (Zones shows one channel, so not there),
  // and what a link that is no traceroute hop comes from.
  const kicker = [
    ...(linkSection ? [link.channel] : []),
    ...(linkKind === "direct" ? ["Heard directly"] : []),
    ...(linkKind === "neighbor" ? ["Neighbour info"] : []),
  ];
  return (
    <>
      <div className="link-direction-heading">
        {/* One line each, cut short; the full name on hover. */}
        <div className="link-ends">
          {[nodeA, nodeB].map((end) => (
            <a
              key={end.node_id}
              className="link-end"
              href={nodeHref(end.node_id)}
              title={nodeLabel(end)}
            >
              {nodeLabel(end)}
            </a>
          ))}
        </div>
        <IconButton
          icon="close"
          label="Clear selected link"
          onClick={onClose}
        />
      </div>
      <section className="link-card" aria-label="Link summary">
        {kicker.length > 0 ? (
          <p className="link-card-channel">{kicker.join(" · ")}</p>
        ) : null}
        <div className="link-card-ways">
          {way(nodeA, nodeB, link.a_to_b, "a_to_b")}
          {way(nodeB, nodeA, link.b_to_a, "b_to_a")}
        </div>
        {linkSection ? (
          <p className="link-card-row">
            <span className="muted">Last seen</span>
            <ShortTime value={link.last_seen_at} />
          </p>
        ) : null}
        <p className="link-card-row">
          {km === null ? (
            <span className="muted" title="A node has no reported position">
              Distance unknown
            </span>
          ) : (
            <>
              <span>
                <strong>{formatKm(km)} km</strong> apart
              </span>
              <a
                className="text-button"
                href={lineOfSightHref(nodeA.node_id, nodeB.node_id)}
              >
                Line of sight <Icon name="profile" />
              </a>
            </>
          )}
        </p>
      </section>
      {linkKind === "traceroute" ? (
        <details className="link-traceroutes">
          <summary>Latest traceroutes</summary>
          {(
            [
              [nodeA, nodeB, "a_to_b"],
              [nodeB, nodeA, "b_to_a"],
            ] as const
          ).map(([from, to, direction]) => (
            <div key={direction}>
              <p className="link-traceroutes-way">
                {nodeLabel(from)} → {nodeLabel(to)}
              </p>
              <HopObservationList
                hops={hops}
                direction={direction}
                fromNodeId={from.node_id}
                toNodeId={to.node_id}
                spreadingFactor={link.spreading_factor}
                compact
                collapsible={false}
              />
            </div>
          ))}
        </details>
      ) : null}
      <p className="link-pages">
        <a
          className="text-button"
          href={linkDetailsHref(linkKind, nodeA.node_id, nodeB.node_id)}
        >
          On the Links page <Icon name="links" />
        </a>
        {linkKind === "traceroute" ? (
          <a
            className="text-button"
            href={tracerouteHopHref(nodeA.node_id, nodeB.node_id)}
          >
            All traceroutes <Icon name="route" />
          </a>
        ) : null}
      </p>
    </>
  );
}

/**
 * One direction in the link card, in large type: the average SNR in its tier
 * colour with the tier named under it (so colour is not the only clue),
 * times heard and, on a traceroute link, whether it spends a hop.
 */
function CardWay({
  linkKind,
  from,
  to,
  direction,
  spreadingFactor,
  hopCost,
}: {
  linkKind: LinkKind;
  from: LinkEnd;
  to: LinkEnd;
  direction: LinkDirection;
  spreadingFactor: number;
  hopCost: ReactNode;
}) {
  const snr = direction.average_snr;
  const tier = linkTier(snr, spreadingFactor);
  const margin = fadeMargin(snr, spreadingFactor);
  const heard = direction.observations;
  return (
    <div className="link-card-way">
      <p
        className="link-card-label"
        title={`${nodeLabel(to)} hearing ${nodeLabel(from)}`}
      >
        {shortLabel(from.node_id, from.short_name)} →{" "}
        {shortLabel(to.node_id, to.short_name)}
      </p>
      <p
        className="link-card-snr"
        style={snr === null ? undefined : { color: linkTierColor(tier) }}
        title={
          margin === null
            ? undefined
            : `Average SNR, ${margin.toFixed(1)} dB above the floor at SF${spreadingFactor}`
        }
      >
        {snr === null ? (
          <span className="muted">—</span>
        ) : (
          <>
            {snr.toFixed(1)}
            <span className="link-card-unit"> dB</span>
          </>
        )}
      </p>
      <p className="link-card-tier">
        {heard === 0
          ? "Not heard"
          : snr === null
            ? "No SNR"
            : linkTierLabel(tier)}
      </p>
      <p className="link-card-seen">
        <strong>{heard.toLocaleString()}</strong> {heardWord(linkKind, heard)}
      </p>
      {hopCost}
    </div>
  );
}

function heardWord(linkKind: LinkKind, count: number): string {
  const word = linkKind === "traceroute" ? "observation" : "report";
  return count === 1 ? word : `${word}s`;
}

// The swatch copies the line's pattern, so colour is not the only clue: dashed
// for a traceroute half without an SNR, dotted for direct links, dash-dot for
// neighbour info.
export function LinkSwatch({
  tier,
  linkKind,
}: {
  tier: LinkTier | null;
  linkKind: LinkKind;
}) {
  const pattern =
    linkKind === "direct"
      ? "link-swatch-dotted"
      : linkKind === "neighbor"
        ? "link-swatch-dash-dot"
        : tier === "unknown"
          ? "link-swatch-dashed"
          : "";
  return (
    <span
      className={`link-swatch ${pattern} ${tier === "unknown" ? "link-swatch-faint" : ""}`}
      style={tier === null ? undefined : { color: linkTierColor(tier) }}
      aria-hidden="true"
    />
  );
}

/**
 * What the link colours mean; the link checkboxes show the patterns. Given a
 * choice, each coloured tier gets a checkbox that shows or hides it. With
 * zeroCost, the 0-hop arrowhead too.
 */
export function LinkQualityLegend({
  choice,
  onChoice,
  zeroCost = false,
}: {
  choice?: LinkTierChoice;
  onChoice?: (choice: LinkTierChoice) => void;
  zeroCost?: boolean;
} = {}) {
  return (
    <ul className="link-quality-legend" aria-label="Link quality">
      {LINK_TIERS.map(({ tier, label, description }) => (
        <li key={tier} title={description}>
          {choice !== undefined &&
          onChoice !== undefined &&
          tier !== "unknown" ? (
            <label>
              <input
                type="checkbox"
                checked={choice[tier]}
                onChange={(event) =>
                  onChoice({ ...choice, [tier]: event.target.checked })
                }
              />
              <LinkSwatch tier={tier} linkKind="traceroute" />
              {label}
            </label>
          ) : (
            <>
              <LinkSwatch tier={tier} linkKind="traceroute" />
              {label}
            </>
          )}
        </li>
      ))}
      {zeroCost ? <ZeroCostKey /> : null}
    </ul>
  );
}

/** A key's entry for the 0-hop arrowhead drawn on hop lines. */
export function ZeroCostKey() {
  return (
    <li title="Passed on without spending a hop">
      <span className="zero-cost-arrow zero-cost-key" aria-hidden="true">
        <span />
      </span>
      0-hop in that direction
    </li>
  );
}
