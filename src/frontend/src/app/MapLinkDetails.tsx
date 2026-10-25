import type { ReactNode } from "react";

import {
  type LinkDirection,
  type LinkKind,
  type MapLink,
  type HopObservations,
  tracerouteHopsUrl,
} from "./api";
import { HopCostLine, HopObservationList } from "./HopObservations";
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
  nodeHref,
  nodeLabel,
  tracerouteHopHref,
} from "./nodeDisplay";
import { DataItem, Icon, IconButton, LinkArrow, Timestamp } from "./ui";
import { type ApiState, useApi } from "./useApi";

/** The names MapLinkDetails shows for a node. */
export interface LinkEnd {
  node_id: number;
  long_name: string | null;
  short_name: string | null;
}

/**
 * A selected link, in the map sidebar or beside the Zones map: both
 * directions, both nodes and how far apart they are now (km, null when either
 * has no reported position). A traceroute link also lists the packets on its
 * channel that reported each direction in the page's window (since). The
 * directions name the nodes, so there is no heading; the close button sits by
 * the first one. Beside the Zones map the Link section shows only the
 * distance (linkSection false): the link's table row already shows the rest.
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
  return (
    <>
      <Direction
        action={
          <IconButton
            icon="close"
            label="Clear selected link"
            onClick={onClose}
          />
        }
        linkKind={linkKind}
        from={nodeA}
        to={nodeB}
        direction={link.a_to_b}
        spreadingFactor={link.spreading_factor}
        hops={hops}
        hopDirection="a_to_b"
      />
      <Direction
        linkKind={linkKind}
        from={nodeB}
        to={nodeA}
        direction={link.b_to_a}
        spreadingFactor={link.spreading_factor}
        hops={hops}
        hopDirection="b_to_a"
      />
      <p className="section-label">Link</p>
      <dl className="node-summary">
        <DataItem
          label="Distance"
          value={
            km === null ? (
              <span className="muted">
                Unknown: a node has no reported position
              </span>
            ) : (
              `${formatKm(km)} km`
            )
          }
        />
        {linkSection ? (
          <>
            <DataItem
              label="Last seen"
              value={<Timestamp value={link.last_seen_at} />}
            />
            <DataItem label="Channel" value={link.channel} />
            <DataItem
              label="Nodes"
              value={
                <>
                  <a className="text-button" href={nodeHref(nodeA.node_id)}>
                    {nodeLabel(nodeA)}
                  </a>
                  <a className="text-button" href={nodeHref(nodeB.node_id)}>
                    {nodeLabel(nodeB)}
                  </a>
                </>
              }
            />
            <DataItem
              label="Terrain"
              value={
                <a
                  className="text-button"
                  href={lineOfSightHref(nodeA.node_id, nodeB.node_id)}
                >
                  Line of sight <Icon name="profile" />
                </a>
              }
            />
            {linkKind === "traceroute" ? (
              <DataItem
                label="Traceroutes"
                value={
                  <a
                    className="text-button"
                    href={tracerouteHopHref(nodeA.node_id, nodeB.node_id)}
                  >
                    Routes
                  </a>
                }
              />
            ) : null}
          </>
        ) : null}
      </dl>
    </>
  );
}

function Direction({
  action,
  linkKind,
  from,
  to,
  direction,
  spreadingFactor,
  hops,
  hopDirection,
}: {
  action?: ReactNode;
  linkKind: LinkKind;
  from: LinkEnd;
  to: LinkEnd;
  direction: LinkDirection;
  spreadingFactor: number;
  hops: ApiState<HopObservations>;
  hopDirection: "a_to_b" | "b_to_a";
}) {
  const snr = direction.average_snr;
  const margin = fadeMargin(snr, spreadingFactor);
  const tier = linkTier(snr, spreadingFactor);
  return (
    <>
      <div className="link-direction-heading">
        <p className="section-label">
          {nodeLabel(from)} <LinkArrow /> {nodeLabel(to)}
          {linkKind === "direct" ? " directly" : null}
          {linkKind === "neighbor" ? " (neighbour info)" : null}
        </p>
        {action}
      </div>
      <dl className="node-summary">
        {/* The tier says when there is no SNR, so only a recorded average
            is shown. */}
        <DataItem
          label="Quality"
          value={
            <>
              <span className="link-tier">
                <LinkSwatch tier={tier} linkKind={linkKind} />
                {linkTierLabel(tier)}
              </span>
              {snr === null ? null : (
                <span title="Average SNR">{snr.toFixed(1)} dB</span>
              )}
              <span className="muted">
                {margin === null
                  ? `SF${spreadingFactor}`
                  : `${margin.toFixed(1)} dB margin at SF${spreadingFactor}`}
              </span>
            </>
          }
        />
        {linkKind === "traceroute" && hops.data !== null ? (
          <DataItem
            label="Hop cost"
            value={
              <HopCostLine
                hops={hops}
                direction={hopDirection}
                name={(nodeId) =>
                  nodeLabel(nodeId === from.node_id ? from : to)
                }
              />
            }
          />
        ) : null}
        {linkKind === "traceroute" ? (
          <DataItem
            label="Traceroutes"
            value={
              <HopObservationList
                hops={hops}
                direction={hopDirection}
                fromNodeId={from.node_id}
                toNodeId={to.node_id}
                spreadingFactor={spreadingFactor}
                compact
              />
            }
          />
        ) : null}
        <DataItem
          label={
            linkKind === "direct"
              ? "Gateway reports"
              : linkKind === "neighbor"
                ? "Reports"
                : "Observations"
          }
          value={`${direction.observations.toLocaleString()} (${direction.snr_samples.toLocaleString()} with SNR)`}
        />
      </dl>
    </>
  );
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
    <li title="Passed on without spending a hop, as for a favourite router">
      <span className="zero-cost-arrow zero-cost-key" aria-hidden="true">
        <span />
      </span>
      0-hop in that direction
    </li>
  );
}
