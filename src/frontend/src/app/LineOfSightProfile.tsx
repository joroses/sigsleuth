import { PointerEvent } from "react";

import {
  CLEAR_RATIO,
  formatDistance,
  formatMeters,
  formatPercent,
  tierRuns,
  type Profile,
  type ProfileSample,
} from "./lineOfSight";

// Drawn like the node page's charts, in SVG user units; the SVG scales to fit.
const WIDTH = 640;
const HEIGHT = 260;
const PLOT = { left: 52, right: 12, top: 12, bottom: 34 };
const PLOT_WIDTH = WIDTH - PLOT.left - PLOT.right;
const PLOT_HEIGHT = HEIGHT - PLOT.top - PLOT.bottom;
const BOTTOM = PLOT.top + PLOT_HEIGHT;

/**
 * Terrain raised by the Earth's curve, with the ground's own elevation as a
 * line under it, the straight sight line with its Fresnel zone (the lower 60%
 * edge drawn), blocked stretches marked under the axis, and a cursor shared
 * with the map.
 */
export function LineOfSightProfile({
  profile,
  hovered,
  onHover,
}: {
  profile: Profile;
  hovered: number | null;
  onHover: (index: number | null) => void;
}) {
  const { samples, distance } = profile;
  const heights = samples.flatMap((sample) => [
    sample.ground,
    sample.terrain,
    sample.sightLine === null ? null : sample.sightLine + sample.fresnel,
    sample.sightLine === null ? null : sample.sightLine - sample.fresnel,
  ]);
  const known = heights.filter((height) => height !== null);
  if (known.length === 0) {
    return <p className="muted">Terrain unavailable for this path.</p>;
  }
  const low = Math.floor(Math.min(...known) / 10) * 10;
  const high = Math.max(low + 10, Math.ceil(Math.max(...known) / 10) * 10);
  const x = (meters: number) =>
    PLOT.left + (distance === 0 ? 0 : (PLOT_WIDTH * meters) / distance);
  const y = (meters: number) =>
    PLOT.top + PLOT_HEIGHT * (1 - (meters - low) / (high - low));
  const line = (
    points: ProfileSample[],
    height: (sample: ProfileSample) => number,
  ) =>
    points
      .map(
        (sample, index) =>
          `${index === 0 ? "M" : "L"}${x(sample.distance).toFixed(1)},${y(height(sample)).toFixed(1)}`,
      )
      .join("");

  const withTerrain = runsOf(samples, (sample) => sample.terrain !== null);
  const withGround = runsOf(samples, (sample) => sample.ground !== null);
  const withSight = samples.filter((sample) => sample.sightLine !== null);
  const blocked = tierRuns(samples).filter((run) => run.tier === "blocked");
  const cursor = hovered === null ? undefined : samples[hovered];

  function pick(event: PointerEvent<SVGSVGElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    const svgX = ((event.clientX - box.left) / box.width) * WIDTH;
    const fraction = (svgX - PLOT.left) / PLOT_WIDTH;
    onHover(
      Math.round(Math.max(0, Math.min(1, fraction)) * (samples.length - 1)),
    );
  }

  return (
    <figure className="activity-chart los-profile">
      <figcaption>
        <h3>Terrain profile</h3>
        <p>
          Terrain raised by the Earth's curve (filled) over the ground's own
          elevation (the light line).
        </p>
      </figcaption>
      <div className="chart-frame">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`Terrain profile over ${formatDistance(distance)}, from ${formatMeters(low)} to ${formatMeters(high)}`}
          onPointerMove={pick}
          onPointerLeave={() => onHover(null)}
        >
          {[low, (low + high) / 2, high].map((tick) => (
            <g key={tick}>
              <line
                className="chart-grid"
                x1={PLOT.left}
                x2={WIDTH - PLOT.right}
                y1={y(tick)}
                y2={y(tick)}
              />
              <text
                className="chart-axis-label"
                x={PLOT.left - 6}
                y={y(tick) + 4}
                textAnchor="end"
              >
                {formatMeters(tick)}
              </text>
            </g>
          ))}
          {[0, distance / 2, distance].map((at, index) => (
            <text
              key={index}
              className="chart-axis-label"
              x={x(at)}
              y={HEIGHT - 6}
              textAnchor={
                index === 0 ? "start" : index === 2 ? "end" : "middle"
              }
            >
              {index === 0
                ? "A"
                : index === 2
                  ? `B · ${formatDistance(at)}`
                  : formatDistance(at)}
            </text>
          ))}
          {withSight.length > 1 ? (
            <path
              className="los-fresnel"
              d={`${line(withSight, (sample) => sample.sightLine! + sample.fresnel)}${line(
                [...withSight].reverse(),
                (sample) => sample.sightLine! - sample.fresnel,
              ).replace(/^M/, "L")}Z`}
            />
          ) : null}
          {withTerrain.map((run) => (
            <path
              key={run[0].distance}
              className="los-terrain"
              d={`${line(run, (sample) => sample.terrain!)}L${x(run.at(-1)!.distance).toFixed(1)},${BOTTOM}L${x(run[0].distance).toFixed(1)},${BOTTOM}Z`}
            />
          ))}
          {withGround.map((run) => (
            <path
              key={run[0].distance}
              className="los-ground"
              d={line(run, (sample) => sample.ground!)}
            />
          ))}
          {withSight.length > 1 ? (
            <>
              <path
                className="los-fresnel-edge"
                d={line(
                  withSight,
                  (sample) => sample.sightLine! - CLEAR_RATIO * sample.fresnel,
                )}
              />
              <path
                className="los-sight"
                d={line(withSight, (s) => s.sightLine!)}
              />
            </>
          ) : null}
          {blocked.map((run) => (
            <rect
              key={run.samples[0].distance}
              className="los-blocked"
              x={x(run.samples[0].distance)}
              y={BOTTOM + 2}
              width={Math.max(
                2,
                x(run.samples.at(-1)!.distance) - x(run.samples[0].distance),
              )}
              height={5}
            >
              <title>Blocked</title>
            </rect>
          ))}
          {cursor !== undefined ? (
            <g>
              <line
                className="chart-crosshair"
                x1={x(cursor.distance)}
                x2={x(cursor.distance)}
                y1={PLOT.top}
                y2={BOTTOM}
              />
              {(
                [
                  [cursor.ground, "#e2cfa6"],
                  [cursor.terrain, "#b39a6b"],
                  [cursor.sightLine, "var(--text)"],
                ] as const
              ).map(([height, fill], index) =>
                height === null ? null : (
                  <circle
                    key={index}
                    className="chart-point"
                    cx={x(cursor.distance)}
                    cy={y(height)}
                    r={4}
                    fill={fill}
                  />
                ),
              )}
            </g>
          ) : null}
        </svg>
        {cursor !== undefined ? (
          <div
            className="chart-tooltip"
            style={{
              left: `${Math.min(80, Math.max(20, (x(cursor.distance) / WIDTH) * 100))}%`,
            }}
          >
            <strong>{formatDistance(cursor.distance)} from A</strong>
            <table>
              <tbody>
                <tr>
                  <td className="number">{formatMeters(cursor.ground)}</td>
                  <td>Ground elevation</td>
                </tr>
                <tr>
                  <td className="number">+{formatMeters(cursor.curve)}</td>
                  <td>Earth's curve</td>
                </tr>
                <tr>
                  <td className="number">{formatMeters(cursor.terrain)}</td>
                  <td>Terrain (with curve)</td>
                </tr>
                <tr>
                  <td className="number">
                    {formatMeters(cursor.sightAltitude)}
                  </td>
                  <td>Sight line altitude (above sea level)</td>
                </tr>
                <tr>
                  <td className="number">{formatMeters(cursor.clearance)}</td>
                  <td>
                    Clearance, {formatPercent(cursor.ratio)} of the Fresnel
                    radius
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    </figure>
  );
}

/** Consecutive samples that pass the test. */
function runsOf(
  samples: ProfileSample[],
  test: (sample: ProfileSample) => boolean,
): ProfileSample[][] {
  const runs: ProfileSample[][] = [];
  let current: ProfileSample[] | null = null;
  for (const sample of samples) {
    if (!test(sample)) {
      current = null;
    } else if (current === null) {
      current = [sample];
      runs.push(current);
    } else {
      current.push(sample);
    }
  }
  return runs;
}
