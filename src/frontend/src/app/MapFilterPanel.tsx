import type { LinkKind } from "./api";
import { MARGIN_STOPS } from "./coverageLayer";
import { demodulationFloor } from "./linkQuality";
import { LinkQualityLegend, LinkSwatch } from "./MapLinkDetails";
import {
  type LinkLayerChoice,
  type LinkTierChoice,
  MAX_HOP_DEPTH,
  MAX_LINK_KM,
} from "./mapLinkLayers";
import { ANY, type FilterOption, type NodeFilter } from "./mapNodeFilter";
import {
  NODE_CATEGORIES,
  NODE_COLORS,
  type NodeCategory,
} from "./mapNodeLayers";
import { SEEN_WINDOWS, type SeenWindow } from "./mapTimeFilter";
import { IconButton } from "./ui";

const CATEGORY_LABELS: Record<NodeCategory, string> = {
  relay: "Router",
  gateway: "Gateway",
  other: "Other",
};

/**
 * What the map draws: the time window, node filters and link layers, with
 * their legends, and the selected node's hop depth. Floats over
 * the map's top left and closes to a button. Link settings show only while a
 * link layer is on.
 */
export function MapFilterPanel({
  open,
  onOpen,
  seenWindow,
  onSeenWindow,
  nodeFilter,
  onNodeFilter,
  categoryCounts,
  roles,
  channels,
  estimatedCount,
  linkChoice,
  onLinkLayer,
  linkTiers,
  onLinkTiers,
  minObservations,
  onMinObservations,
  minKm,
  onMinKm,
  showTextReach,
  onShowTextReach,
  showCoverage,
  onShowCoverage,
  coverageSpreadingFactor,
  hopDepth,
  onHopDepth,
  nearbyShown,
}: {
  open: boolean;
  onOpen: (open: boolean) => void;
  seenWindow: SeenWindow;
  onSeenWindow: (window: SeenWindow) => void;
  nodeFilter: NodeFilter;
  onNodeFilter: (filter: NodeFilter) => void;
  categoryCounts: Record<NodeCategory, number>;
  roles: FilterOption[];
  channels: FilterOption[];
  /** Nodes drawn at an estimate that the estimated checkbox would show. */
  estimatedCount: number;
  linkChoice: LinkLayerChoice;
  onLinkLayer: (linkKind: LinkKind, shown: boolean) => void;
  linkTiers: LinkTierChoice;
  onLinkTiers: (choice: LinkTierChoice) => void;
  /** Kept as typed, so the box can be emptied. */
  minObservations: string;
  onMinObservations: (value: string) => void;
  minKm: number;
  onMinKm: (km: number) => void;
  showTextReach: boolean;
  onShowTextReach: (shown: boolean) => void;
  showCoverage: boolean;
  onShowCoverage: (shown: boolean) => void;
  /** The one spreading factor of the cells in view, else null. */
  coverageSpreadingFactor: number | null;
  hopDepth: number;
  onHopDepth: (depth: number) => void;
  /** Nodes kept by the hop depth besides the selected one; null with none selected. */
  nearbyShown: number | null;
}) {
  const anyLinkLayer = Object.values(linkChoice).some(Boolean);
  if (!open)
    return (
      <div className="map-filter-panel map-filter-panel-closed">
        <IconButton
          icon="filter"
          label="Show map filters"
          aria-expanded={false}
          onClick={() => onOpen(true)}
        />
      </div>
    );
  return (
    <section className="map-filter-panel" aria-label="Map filters">
      <div className="map-filter-heading">
        <h2 className="section-label">Map filters</h2>
        <IconButton
          icon="close"
          label="Hide map filters"
          aria-expanded={true}
          onClick={() => onOpen(false)}
        />
      </div>
      <div className="map-filter-body">
        <div className="map-filter-row">
          <label htmlFor="map-seen-window-select">Last seen within</label>
          <select
            id="map-seen-window-select"
            value={seenWindow}
            onChange={(event) => onSeenWindow(event.target.value as SeenWindow)}
          >
            {SEEN_WINDOWS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="map-filter-row">
          <label htmlFor="map-role-select">Role</label>
          <FilterSelect
            id="map-role-select"
            allLabel="All roles"
            options={roles}
            value={nodeFilter.role}
            onChange={(role) => onNodeFilter({ ...nodeFilter, role })}
          />
        </div>
        <div className="map-filter-row">
          <label htmlFor="map-channel-select">Channel</label>
          <FilterSelect
            id="map-channel-select"
            allLabel="All channels"
            options={channels}
            value={nodeFilter.channel}
            onChange={(channel) => onNodeFilter({ ...nodeFilter, channel })}
          />
        </div>
        <fieldset className="map-filter-checks">
          <legend className="section-label">Nodes</legend>
          {NODE_CATEGORIES.map((category) => (
            <label key={category}>
              <input
                type="checkbox"
                checked={nodeFilter.categories[category]}
                onChange={(event) =>
                  onNodeFilter({
                    ...nodeFilter,
                    categories: {
                      ...nodeFilter.categories,
                      [category]: event.target.checked,
                    },
                  })
                }
              />
              <NodeSwatch color={NODE_COLORS[category]} />
              <span className="map-filter-name">
                {CATEGORY_LABELS[category]}
              </span>
              <span className="map-filter-count">
                {categoryCounts[category].toLocaleString()}
              </span>
            </label>
          ))}
          <label title="No position: placed near the nodes that hear it">
            <input
              type="checkbox"
              checked={nodeFilter.estimated}
              onChange={(event) =>
                onNodeFilter({ ...nodeFilter, estimated: event.target.checked })
              }
            />
            <NodeSwatch color="#64748b" hollow />
            <span className="map-filter-name">Estimated positions</span>
            <span className="map-filter-count">
              {estimatedCount.toLocaleString()}
            </span>
          </label>
        </fieldset>
        <fieldset className="map-filter-checks">
          <legend className="section-label">Links</legend>
          <label title="Hop reported in a traceroute">
            <input
              type="checkbox"
              checked={linkChoice.traceroute}
              onChange={(event) =>
                onLinkLayer("traceroute", event.target.checked)
              }
            />
            <LinkSwatch tier={null} linkKind="traceroute" />
            <span className="map-filter-name">Traceroute links</span>
          </label>
          <label title="Heard directly by a gateway">
            <input
              type="checkbox"
              checked={linkChoice.direct}
              onChange={(event) => onLinkLayer("direct", event.target.checked)}
            />
            <LinkSwatch tier={null} linkKind="direct" />
            <span className="map-filter-name">Heard directly</span>
          </label>
          <label title="From neighbour reports">
            <input
              type="checkbox"
              checked={linkChoice.neighbor}
              onChange={(event) =>
                onLinkLayer("neighbor", event.target.checked)
              }
            />
            <LinkSwatch tier={null} linkKind="neighbor" />
            <span className="map-filter-name">Neighbour info</span>
          </label>
          <label title="Share of its texts each gateway heard">
            <input
              type="checkbox"
              checked={showTextReach}
              onChange={(event) => onShowTextReach(event.target.checked)}
            />
            <span className="text-reach-swatch" aria-hidden="true">
              %
            </span>
            <span className="map-filter-name">Text reach</span>
          </label>
          {anyLinkLayer ? (
            <>
              <label className="min-observations">
                Observed at least
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={minObservations}
                  onChange={(event) => onMinObservations(event.target.value)}
                />
                times
              </label>
              <label className="min-km">
                At least
                <input
                  type="range"
                  min={0}
                  max={MAX_LINK_KM}
                  step={5}
                  value={minKm}
                  onChange={(event) => onMinKm(Number(event.target.value))}
                />
                <output>{minKm} km</output>
              </label>
              <div
                className="hop-depth"
                title={nearbyShown === null ? "Select a node first" : undefined}
              >
                <label htmlFor="map-hop-depth">Hop depth</label>
                <input
                  id="map-hop-depth"
                  type="range"
                  min={0}
                  max={MAX_HOP_DEPTH}
                  step={1}
                  value={hopDepth}
                  disabled={nearbyShown === null}
                  onChange={(event) => onHopDepth(Number(event.target.value))}
                />
                <output htmlFor="map-hop-depth">{hopDepth}</output>
                {nearbyShown === null ? null : (
                  <span
                    className="hop-depth-note"
                    title={
                      hopDepth === 0
                        ? "Nodes linked directly to it"
                        : `Nodes within ${hopDepth} ${hopDepth === 1 ? "relay" : "relays"} of it`
                    }
                  >
                    {nearbyShown.toLocaleString()}{" "}
                    {nearbyShown === 1 ? "node" : "nodes"}
                  </span>
                )}
              </div>
              <LinkQualityLegend
                choice={linkTiers}
                onChoice={onLinkTiers}
                zeroCost={linkChoice.traceroute}
              />
            </>
          ) : null}
        </fieldset>
        <fieldset className="map-filter-checks">
          <legend className="section-label">Signal surveys</legend>
          <label title="Where survey phones heard packets, by signal">
            <input
              type="checkbox"
              checked={showCoverage}
              onChange={(event) => onShowCoverage(event.target.checked)}
            />
            <span className="coverage-swatch" aria-hidden="true" />
            <span className="map-filter-name">Coverage</span>
          </label>
          {showCoverage ? (
            <CoverageLegend spreadingFactor={coverageSpreadingFactor} />
          ) : null}
        </fieldset>
      </div>
    </section>
  );
}

// The cells' margin scale, from the edge of decoding to a strong signal. With
// one spreading factor, margin is SNR minus that factor's floor, so the same
// colours read as SNR.
function CoverageLegend({
  spreadingFactor,
}: {
  spreadingFactor: number | null;
}) {
  const floor =
    spreadingFactor === null ? null : demodulationFloor(spreadingFactor);
  const gradient = MARGIN_STOPS.map(
    ([db, color]) => `${color} ${(db / 25) * 100}%`,
  ).join(", ");
  return (
    <div className="coverage-legend">
      <span
        className="coverage-legend-bar"
        style={{ background: `linear-gradient(to right, ${gradient})` }}
        aria-hidden="true"
      />
      <span className="coverage-legend-scale">
        {floor === null ? (
          <>
            <span>0 dB</span>
            <span>above the decoding limit</span>
            <span>25 dB</span>
          </>
        ) : (
          <>
            <span>{floor} dB</span>
            <span>average SNR</span>
            <span>{floor + 25} dB</span>
          </>
        )}
      </span>
    </div>
  );
}

// Options come from the loaded nodes, so a choice kept after a reload may have
// none left; it stays listed with a count of 0.
function FilterSelect({
  id,
  allLabel,
  options,
  value,
  onChange,
}: {
  id: string;
  allLabel: string;
  options: FilterOption[];
  value: string;
  onChange: (value: string) => void;
}) {
  const shown =
    value === ANY || options.some((option) => option.value === value)
      ? options
      : [...options, { value, label: value, count: 0 }];
  return (
    <select
      id={id}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value={ANY}>{allLabel}</option>
      {shown.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label} ({option.count.toLocaleString()})
        </option>
      ))}
    </select>
  );
}

function NodeSwatch({
  color,
  hollow = false,
}: {
  color: string;
  hollow?: boolean;
}) {
  return (
    <span
      className="node-swatch"
      style={
        hollow
          ? { backgroundColor: "#ffffff", borderColor: color }
          : { backgroundColor: color }
      }
      aria-hidden="true"
    />
  );
}
