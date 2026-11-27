import { Fragment } from "react";

import type {
  CoverageCellDetail,
  CoverageCellNetwork,
  CoverageHeardNode,
} from "./api";
import { heardHowText, marginColor } from "./coverageLayer";
import { nodeLabel, relayByteText } from "./nodeDisplay";
import { DataItem, LegendLine } from "./ui";
import type { ApiState } from "./useApi";

const dayFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

/** A UTC date such as "2026-09-25", as the backend sends it. */
function Day({ value }: { value: string }) {
  return <time dateTime={value}>{dayFormat.format(new Date(value))}</time>;
}

function snr(value: number | null): string {
  return value === null ? "no SNR" : `${value.toFixed(1)} dB`;
}

/** A network's radio settings, such as "SF7 · 62.5 kHz · 869.618" (MHz). */
function networkLabel(network: CoverageCellNetwork): string {
  const parts = [
    network.spreading_factor === null ? null : `SF${network.spreading_factor}`,
    network.bandwidth_khz === null ? null : `${network.bandwidth_khz} kHz`,
    network.frequency_mhz === null ? null : `${network.frequency_mhz}`,
  ].filter((part) => part !== null);
  return parts.length === 0 ? "unknown settings" : parts.join(" · ");
}

/**
 * The card for a clicked coverage cell: its size and totals, the nodes heard
 * there and how each was identified, and unknown relays. Choosing a heard
 * node selects it on the map.
 *
 * It shows average SNR as radios report it. SNR means different things at
 * different spreading factors, so each radio setting surveyed in the cell
 * gets its own SNR line.
 */
export function CoverageCellDetails({
  detail,
  isDrawn,
  onChooseNode,
}: {
  detail: ApiState<CoverageCellDetail>;
  /** Heard nodes the map draws can be chosen; others link to their page. */
  isDrawn: (nodeId: number) => boolean;
  onChooseNode: (nodeId: number) => void;
}) {
  const cell = detail.data;
  return (
    <section
      className="selected-node"
      aria-live="polite"
      aria-label="Coverage here"
    >
      <p className="muted coverage-cell-size">
        {cell === null
          ? null
          : `Cell about ${cell.size_m.toLocaleString()} m across`}
      </p>
      {detail.error !== null ? (
        <p className="sidebar-note">The cell could not load. {detail.error}</p>
      ) : cell === null ? (
        <p className="sidebar-note">Loading…</p>
      ) : cell.measurements === 0 ? (
        <p className="sidebar-note">No measurements here.</p>
      ) : (
        <>
          <dl className="node-summary">
            <DataItem
              label="Measurements"
              value={cell.measurements.toLocaleString()}
            />
            {cell.networks.map((network) => (
              <DataItem
                key={networkLabel(network)}
                label="SNR"
                value={
                  <NetworkSnr
                    network={network}
                    counted={cell.networks.length > 1}
                  />
                }
              />
            ))}
            {cell.first_heard_on === null ||
            cell.last_heard_on === null ? null : (
              <DataItem
                label="Heard"
                value={
                  cell.first_heard_on === cell.last_heard_on ? (
                    <Day value={cell.first_heard_on} />
                  ) : (
                    <>
                      <Day value={cell.first_heard_on} /> to{" "}
                      <Day value={cell.last_heard_on} />
                    </>
                  )
                }
              />
            )}
          </dl>
          <p className="section-label">Heard here</p>
          {cell.heard_nodes.length === 0 && cell.unknown_relays.length === 0 ? (
            <p className="sidebar-note">No heard nodes.</p>
          ) : (
            <ul className="coverage-heard" aria-label="Nodes heard here">
              {cell.heard_nodes.map((node) => (
                <HeardNodeItem
                  key={node.node_id}
                  node={node}
                  onChoose={isDrawn(node.node_id) ? onChooseNode : null}
                />
              ))}
              {cell.unknown_relays.map((relay) => (
                <li key={relay.relay_byte ?? "none"}>
                  <span className="muted">
                    {relay.relay_byte === null || relay.relay_byte === 0
                      ? "No relay byte"
                      : `Unknown relay ending in ${relayByteText(relay.relay_byte)}`}
                  </span>
                  <span className="number">
                    {relay.measurements.toLocaleString()}
                  </span>
                  {relay.candidates.length === 0 ? null : (
                    <span className="muted coverage-how">
                      Maybe{" "}
                      {relay.candidates.map((node, index) => (
                        <Fragment key={node.node_id}>
                          {index === 0 ? null : ", "}
                          <button
                            type="button"
                            className={
                              index === 0
                                ? "text-button coverage-first-hint"
                                : "text-button"
                            }
                            onClick={() => onChooseNode(node.node_id)}
                          >
                            {nodeLabel(node)}
                          </button>
                          {node.estimated ? null : ` (${node.km} km)`}
                        </Fragment>
                      ))}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <ul
            className="coverage-line-legend"
            aria-label="Lines to heard nodes"
          >
            <li>
              <LegendLine dashes={undefined} />
              Heard directly
            </li>
            <li>
              <LegendLine dashes="4 4" />
              Likely relay
            </li>
          </ul>
        </>
      )}
    </section>
  );
}

/** A network's average SNR and its radio settings. */
function NetworkSnr({
  network,
  counted,
}: {
  network: CoverageCellNetwork;
  /** Whether to say how many of the cell's measurements it has. */
  counted: boolean;
}) {
  return (
    <>
      <MarginDot margin={network.average_margin} />
      {snr(network.average_snr)} average
      <span className="muted">
        {" "}
        {counted
          ? `(${network.measurements.toLocaleString()} on ${networkLabel(network)})`
          : networkLabel(network)}
      </span>
    </>
  );
}

/** A margin's colour on the cells' scale, beside the SNR it comes from. */
function MarginDot({ margin }: { margin: number | null }) {
  return (
    <span
      className="coverage-margin-dot"
      style={{ background: marginColor(margin) }}
      title={
        margin === null
          ? "No SNR"
          : `${margin.toFixed(1)} dB above the decoding limit, coloured as the cells`
      }
    />
  );
}

/** A line to a heard node as the map draws it: solid, or dashed for a guess. */
function HeardNodeItem({
  node,
  onChoose,
}: {
  node: CoverageHeardNode;
  onChoose: ((nodeId: number) => void) | null;
}) {
  const label = nodeLabel(node);
  return (
    <li>
      {onChoose === null ? (
        <a className="text-button" href={`#nodes/${node.node_id}`}>
          {label}
        </a>
      ) : (
        <button
          type="button"
          className="text-button"
          title={`Show where ${label} was heard`}
          onClick={() => onChoose(node.node_id)}
        >
          {label}
        </button>
      )}
      <span className="number">
        {node.measurements.toLocaleString()} ·{" "}
        <MarginDot margin={node.average_margin} />
        {snr(node.average_snr)} SNR
      </span>
      <span className="muted coverage-how">
        {heardHowText(node)}
        {node.channel === null ? null : ` · ${node.channel}`}
      </span>
    </li>
  );
}
