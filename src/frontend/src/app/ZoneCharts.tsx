import type { NetworkZones } from "./api";
import { DayTotalChart } from "./NodeActivityCharts";
import { firstChartDay, packetsByDay } from "./nodeActivity";

// The Network network page's two charts. Unlike the rest of the page, both
// cover every channel.

// Channels past this many are added up in one "other channels" bar.
const CHANNEL_BARS = 8;

export function ZoneCharts({ network }: { network: NetworkZones }) {
  // The node page's local-day buckets, with every hour counted as text.
  const days = packetsByDay(
    network.text_hours.map((hour) => ({
      hour_start: hour.hour_start,
      portnum: 1,
      port: "TEXT_MESSAGE_APP",
      packets: hour.count,
    })),
    firstChartDay(new Date(network.generated_at)),
  );
  return (
    <div className="activity-charts">
      <figure className="activity-chart">
        <figcaption>
          <h3>Broadcast texts per day</h3>
          <p>Last 7 days, any channel.</p>
        </figcaption>
        <DayTotalChart
          days={days}
          what="broadcast texts"
          color="var(--series-1)"
        />
      </figure>
      <figure className="activity-chart">
        <figcaption>
          <h3>Active nodes by channel</h3>
          <p>Last 24 hours, by the channel of their latest packet.</p>
        </figcaption>
        <ChannelBars channels={network.active_channels} />
      </figure>
    </div>
  );
}

/** Horizontal bars, busiest channel first, each with its count at the tip. */
function ChannelBars({
  channels,
}: {
  channels: NetworkZones["active_channels"];
}) {
  if (channels.length === 0) {
    return <p className="muted">No nodes seen in the last 24 hours.</p>;
  }
  const bars: { label: string; nodes: number; other: boolean }[] = channels
    .slice(0, CHANNEL_BARS)
    .map((channel) => ({
      label: channel.channel,
      nodes: channel.nodes,
      other: false,
    }));
  const rest = channels.slice(CHANNEL_BARS);
  if (rest.length > 0) {
    bars.push({
      label: `${rest.length} other ${rest.length === 1 ? "channel" : "channels"}`,
      nodes: rest.reduce((sum, channel) => sum + channel.nodes, 0),
      other: true,
    });
  }
  const max = Math.max(1, ...bars.map((bar) => bar.nodes));
  return (
    <ul className="zones-bars">
      {bars.map((bar) => (
        <li key={bar.label} title={`${bar.label}: ${bar.nodes} nodes`}>
          <span className="zones-bar-label">{bar.label}</span>
          <span className="zones-bar-track">
            <span
              className="zones-bar"
              style={{
                // Room is left at the end of the longest bar for its count.
                width: `calc((100% - 4rem) * ${bar.nodes / max})`,
                background: bar.other
                  ? "var(--series-other)"
                  : "var(--series-3)",
              }}
            />
            <span className="zones-bar-value">
              {bar.nodes.toLocaleString()}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}
