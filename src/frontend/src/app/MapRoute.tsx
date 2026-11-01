import type { MapLink, MapNode } from "./api";
import { fadeMargin } from "./linkQuality";
import { drawnKm } from "./mapLinkLayers";
import { formatKm, formatNodeId, nodeLabel, shortLabel } from "./nodeDisplay";
import { BothWays, DirectionSnr, Icon } from "./ui";

interface HopStep {
  key: string;
  /** Turned so node_a is the near end. */
  link: MapLink;
}

// One row per link, named by its far end: times heard and SNR each way,
// out first, and the length between reported positions.
export function HopTable({
  steps,
  nodes,
  onOpen,
}: {
  steps: HopStep[];
  nodes: ReadonlyMap<number, MapNode>;
  onOpen: (key: string) => void;
}) {
  return (
    <table className="hop-table">
      <thead>
        <tr>
          <th scope="col">Node</th>
          <th scope="col" title="Times heard out and in">
            Seen
          </th>
          <th scope="col" title="Average SNR out and in, in dB">
            SNR
          </th>
          <th scope="col">km</th>
        </tr>
      </thead>
      <tbody>
        {steps.map(({ key, link }) => {
          const near = nodes.get(link.node_a)!;
          const far = nodes.get(link.node_b)!;
          const nearName = shortLabel(near.node_id, near.short_name);
          const farName = shortLabel(far.node_id, far.short_name);
          const km =
            near.estimate === null && far.estimate === null
              ? drawnKm(near, far)
              : null;
          return (
            <tr
              key={key}
              onClick={(event) => {
                if ((event.target as Element).closest("button")) return;
                onOpen(key);
              }}
            >
              <td>
                <button
                  type="button"
                  className="text-button"
                  title={nodeLabel(far)}
                  aria-label={`Show link to ${nodeLabel(far)}`}
                  onClick={() => onOpen(key)}
                >
                  {farName}
                </button>
              </td>
              <td>
                <BothWays
                  left={link.a_to_b.observations.toLocaleString()}
                  right={link.b_to_a.observations.toLocaleString()}
                />
              </td>
              <td>
                <BothWays
                  left={
                    <DirectionSnr
                      direction={link.a_to_b}
                      spreadingFactor={link.spreading_factor}
                      label={`${nearName} → ${farName}`}
                    />
                  }
                  right={
                    <DirectionSnr
                      direction={link.b_to_a}
                      spreadingFactor={link.spreading_factor}
                      label={`${farName} → ${nearName}`}
                    />
                  }
                />
              </td>
              <td>{formatKm(km)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * A route from the selected node: picking its other end, then the route's
 * links, or why none was found.
 */
export function MapRoute({
  from,
  toId,
  steps,
  hops,
  nodes,
  onOpen,
  onChangeEnd,
  onReverse,
}: {
  from: MapNode;
  toId: number | null;
  steps: HopStep[] | null;
  /** Hops spent, as Meshtastic counts them. */
  hops: number;
  nodes: ReadonlyMap<number, MapNode>;
  onOpen: (key: string) => void;
  onChangeEnd: () => void;
  onReverse: (to: MapNode) => void;
}) {
  if (toId === null)
    return (
      <section className="map-route" aria-live="polite">
        <h3>Route from {nodeLabel(from)}</h3>
        <p className="sidebar-note">
          Search or select another node to find a likely route.
        </p>
      </section>
    );
  const to = nodes.get(toId);
  const heading = (
    <h3>
      {nodeLabel(from)} →{" "}
      {to === undefined ? formatNodeId(toId) : nodeLabel(to)}
    </h3>
  );
  const change = (
    <button type="button" className="text-button" onClick={onChangeEnd}>
      <Icon name="route" /> Change destination
    </button>
  );
  if (to === undefined || steps === null)
    return (
      <section className="map-route" aria-live="polite">
        {heading}
        <p className="sidebar-note">
          {to === undefined
            ? "That node is not on the map with the current filters."
            : "No route over these link layers and filters."}
        </p>
        {change}
      </section>
    );
  const relays = steps.length - 1;
  // The weakest SNR the way travelled, by margin.
  let weakest: MapLink | null = null;
  let weakestMargin = Infinity;
  for (const { link } of steps) {
    const margin = fadeMargin(link.a_to_b.average_snr, link.spreading_factor);
    if (margin !== null && margin < weakestMargin) {
      weakest = link;
      weakestMargin = margin;
    }
  }
  return (
    <section className="map-route" aria-live="polite">
      {heading}
      <p className="map-route-summary">
        {hops} {hops === 1 ? "hop" : "hops"}
        {` · ${relays} ${relays === 1 ? "relay" : "relays"}`}
        {weakest === null ? null : (
          <>
            {" · weakest "}
            <DirectionSnr
              direction={weakest.a_to_b}
              spreadingFactor={weakest.spreading_factor}
            />
            {" dB SNR"}
          </>
        )}
      </p>
      <HopTable steps={steps} nodes={nodes} onOpen={onOpen} />
      <div className="map-route-actions">
        {change}
        <button
          type="button"
          className="text-button"
          onClick={() => onReverse(to)}
        >
          <Icon name="swap" /> Reverse
        </button>
      </div>
    </section>
  );
}
