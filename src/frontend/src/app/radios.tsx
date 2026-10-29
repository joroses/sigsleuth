// Node numbers several radios use at once (shared_numbers.py): the radio a
// packet is sorted to, a node page's radios and its filter.

import type { NodeOverview, PacketRow } from "./api";
import {
  formatHardware,
  formatNodeId,
  nodeHref,
  radioName,
} from "./nodeDisplay";
import { Badge, DataItem, ShortTime } from "./ui";

export type NodeRadios = NonNullable<NodeOverview["radios"]>;
export type Radio = NodeRadios["items"][number];
type SenderRadio = NonNullable<PacketRow["sender_radio"]>;
/** A node packets filter: a radio's number, or packets sorted to none. */
export type RadioFilter = number | "unsorted";

const SORT_REASONS: Record<NonNullable<SenderRadio["reason"]>, string> = {
  node_info: "Its own NodeInfo",
  gateways: "Only this radio's gateways heard it",
  channel: "Only this radio uses its channel",
};

/** Under a packet's sender: the radio it is sorted to, or Unsorted. */
export function SenderRadioLine({ radio }: { radio: SenderRadio | null }) {
  if (radio === null) return null;
  return radio.reason === null || radio.number === null ? (
    <span className="data-line muted" title="Not sorted to one radio">
      Unsorted
    </span>
  ) : (
    <span className="data-line muted" title={SORT_REASONS[radio.reason]}>
      {radio.name ?? `Radio ${radio.number}`}
    </span>
  );
}

/** Beside a distance with an end that is a shared node number. */
export function SharedBadge() {
  return (
    <Badge tone="amber" title="Several radios use this node number">
      shared
    </Badge>
  );
}

/** Picks a radio's packets, or the unsorted ones; null shows every packet. */
export function RadioChips({
  radios,
  radio,
  onChange,
}: {
  radios: NodeRadios;
  radio: RadioFilter | null;
  onChange: (radio: RadioFilter | null) => void;
}) {
  const chip = (value: RadioFilter | null, label: string, count?: number) => (
    <button
      key={String(value)}
      type="button"
      className="chat-channel-chip"
      aria-pressed={radio === value}
      onClick={() => onChange(value)}
    >
      {label}
      {count === undefined ? null : (
        <span className="muted"> {count.toLocaleString()}</span>
      )}
    </button>
  );
  return (
    <div className="radio-chips" role="group" aria-label="Radio">
      {chip(null, "All radios")}
      {radios.items.map((item) =>
        chip(item.number, radioName(item), item.sorted_packets ?? undefined),
      )}
      {chip("unsorted", "Unsorted", radios.unsorted_packets ?? undefined)}
    </div>
  );
}

/** A shared node number's radios, one card each. */
export function RadioCards({ radios }: { radios: NodeRadios }) {
  return (
    <section className="radio-cards" aria-label="Radios">
      <p className="muted">
        From firmware 2.8 the node number comes from the key, so radios with the
        same key share it; a new key gives a radio its own number. Sorting
        packets to radios is an approximation.
      </p>
      <div className="radio-card-list">
        {radios.items.map((radio) => (
          <article key={radio.number} className="radio-card">
            <h3>
              <span className="muted">{radio.number}</span> {radioName(radio)}
            </h3>
            <dl>
              <DataItem
                label="Hardware"
                value={formatHardware(radio.hardware_model)}
              />
              <DataItem
                label="Place"
                value={
                  radio.position === null ? (
                    "Not reported"
                  ) : (
                    <a
                      className="text-button mono"
                      href={`#packets/${radio.position.packet_row_id}`}
                    >
                      {radio.position.latitude.toFixed(4)},{" "}
                      {radio.position.longitude.toFixed(4)}
                    </a>
                  )
                }
              />
              <DataItem label="Heard" value={<RadioSpan radio={radio} />} />
              <DataItem
                label="Sorted packets"
                value={(radio.sorted_packets ?? 0).toLocaleString()}
              />
            </dl>
            <LaterNumber radio={radio} />
          </article>
        ))}
      </div>
    </section>
  );
}

/** A number's radios that took turns: when each reported. */
export function RadioItems({ radios }: { radios: NodeRadios }) {
  return radios.items.map((radio) => (
    <DataItem
      key={radio.number}
      label={radioName(radio)}
      value={
        <>
          <span className="data-line">
            <RadioSpan radio={radio} />
          </span>
          <LaterNumber radio={radio} />
        </>
      }
    />
  ));
}

function RadioSpan({ radio }: { radio: Radio }) {
  return (
    <>
      <ShortTime value={radio.first_reported_at} /> –{" "}
      <ShortTime value={radio.last_reported_at} />
    </>
  );
}

function LaterNumber({ radio }: { radio: Radio }) {
  if (radio.later_node_id === null) return null;
  return (
    <span className="data-line">
      now{" "}
      <a className="text-button mono" href={nodeHref(radio.later_node_id)}>
        {formatNodeId(radio.later_node_id)}
      </a>
    </span>
  );
}
