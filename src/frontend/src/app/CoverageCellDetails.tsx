import type {
  CoverageCellDetail,
  CoverageCellNetwork,
  CoverageHeardNode,
  CoverageMeasurementRow,
} from "./api";
import { heardHowText, isCertain, marginColor } from "./coverageLayer";
import { formatNodeId, nodeLabel, relayByteText } from "./nodeDisplay";
import { DataItem, IconButton, LegendLine, ShortTime, Timestamp } from "./ui";
import type { ApiState } from "./useApi";

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
 * The "Coverage here" card for a clicked coverage cell: its totals, the nodes
 * heard there and how each was identified, unknown relays and the newest
 * measurements. Choosing a heard node selects it on the map.
 *
 * It shows average SNR as radios report it. SNR means different things at
 * different spreading factors, so each radio setting surveyed in the cell
 * gets its own SNR line.
 */
export function CoverageCellDetails({
  detail,
  onlyOf,
  isDrawn,
  onChooseNode,
  onClose,
}: {
  detail: ApiState<CoverageCellDetail>;
  /** How the selected node narrows the cell, or null for every node. */
  onlyOf: string | null;
  /** Heard nodes the map draws can be chosen; others link to their page. */
  isDrawn: (nodeId: number) => boolean;
  onChooseNode: (nodeId: number) => void;
  onClose: () => void;
}) {
  const cell = detail.data;
  return (
    <section
      className="selected-node sidebar-selection"
      aria-live="polite"
      aria-label="Coverage here"
    >
      <div className="selected-node-heading">
        <div>
          <h3>Coverage here</h3>
          {cell === null ? null : (
            <span className="muted">
              Cell about {cell.size_m.toLocaleString()} m across
            </span>
          )}
          {onlyOf === null ? null : <div className="muted">{onlyOf}</div>}
        </div>
        <IconButton
          icon="close"
          label="Clear coverage cell"
          onClick={onClose}
        />
      </div>
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
            {cell.first_heard_at === null ||
            cell.last_heard_at === null ? null : (
              <DataItem
                label="Heard"
                value={
                  <>
                    <Timestamp value={cell.first_heard_at} /> to{" "}
                    <Timestamp value={cell.last_heard_at} />
                  </>
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
                      {relay.candidates
                        .map((node) => `${nodeLabel(node)} (${node.km} km)`)
                        .join(", ")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <details className="hop-observations-toggle">
            <summary>Latest {cell.newest.length} measurements</summary>
            <ul className="hop-observations" aria-label="Latest measurements">
              {cell.newest.map((row, index) => (
                <MeasurementItem key={index} row={row} />
              ))}
            </ul>
          </details>
          <ul
            className="coverage-line-legend"
            aria-label="Lines to heard nodes"
          >
            <li>
              <LegendLine dashes={undefined} />
              Heard directly by a survey
            </li>
            <li>
              <LegendLine dashes="4 4" />
              Guessed from MQTT, or the only node that could have relayed it
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

/** A measurement, by the node the survey node heard: the sender or a relay. */
function MeasurementItem({ row }: { row: CoverageMeasurementRow }) {
  const time = <ShortTime value={row.heard_at} />;
  return (
    <li>
      {row.packet_row_id === null ? (
        time
      ) : (
        <a className="text-button" href={`#packets/${row.packet_row_id}`}>
          {time}
        </a>
      )}
      <span className="number">
        <MarginDot margin={row.margin} />
        {row.rx_snr === null ? "? dB" : `${row.rx_snr.toFixed(1)} dB`}
        {row.rx_rssi === null ? null : ` ${row.rx_rssi} dBm`}
      </span>
      <span className="hop-observation-run">
        {row.heard_node_id !== null ? (
          <>
            {row.heard_name ?? formatNodeId(row.heard_node_id)}
            {!isCertain(row.heard_how) ? (
              <span className="muted"> (guess)</span>
            ) : null}
          </>
        ) : (
          <span className="muted">
            {row.relay_byte === null || row.relay_byte === 0
              ? "No relay byte"
              : `Unknown relay ending in ${relayByteText(row.relay_byte)}`}
          </span>
        )}
      </span>
    </li>
  );
}
