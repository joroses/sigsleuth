import { CSSProperties, MouseEvent } from "react";

import { PacketPage, PacketPort, PacketRow } from "./api";
import { portGroup } from "./nodeActivity";
import { SenderRadioLine } from "./radios";
import { PagedApiState, usePagedApi } from "./useApi";
import { ErrorPanel, NodeLink, StatusPanel, ShortTime } from "./ui";

/** Picks one application port to list packets on; "All ports" is null. */
export function PortSelect({
  ports,
  port,
  onChange,
}: {
  ports: PacketPort[];
  port: number | null;
  onChange: (port: number | null) => void;
}) {
  if (ports.length === 0) {
    return null;
  }
  return (
    <div className="nodes-toolbar">
      <select
        aria-label="Port"
        value={port ?? ""}
        onChange={(event) =>
          onChange(
            event.target.value === "" ? null : Number(event.target.value),
          )
        }
      >
        <option value="">All ports</option>
        {ports.map((item) => (
          <option key={item.portnum} value={item.portnum}>
            {item.port}
          </option>
        ))}
      </select>
    </div>
  );
}

/** Loads the packets at a URL that pages with before_id and shows them in a PacketTable. */
export function PagedPacketTable({
  url,
  empty,
}: {
  url: string;
  empty: string;
}) {
  const { data, error, ...paging } = usePagedApi<PacketPage>(
    url,
    appendPackets,
  );
  if (error !== null) {
    return <StatusPanel tone="error">{error}</StatusPanel>;
  }
  if (data === null) {
    return <StatusPanel>Loading packets…</StatusPanel>;
  }
  return (
    <PacketTable
      packets={data.packets}
      nextBeforeId={data.next_before_id}
      paging={paging}
      empty={empty}
    />
  );
}

function appendPackets(loaded: PacketPage, next: PacketPage): PacketPage {
  return { ...next, packets: [...loaded.packets, ...next.packets] };
}

/**
 * A page of packets newest first, with "Load more packets" below. The packet
 * ID links to the packet; a click elsewhere on the row opens it too.
 */
export function PacketTable({
  packets,
  nextBeforeId,
  paging,
  empty,
}: {
  packets: PacketRow[];
  nextBeforeId: number | null;
  paging: Pick<
    PagedApiState<unknown>,
    "loadingMore" | "loadMoreError" | "loadMore"
  >;
  empty: string;
}) {
  if (packets.length === 0) {
    return <StatusPanel>{empty}</StatusPanel>;
  }
  return (
    <>
      <div className="table-wrap">
        <table className="packet-table">
          <thead>
            <tr>
              <th scope="col">Time</th>
              <th scope="col">Packet ID</th>
              <th scope="col">From</th>
              <th scope="col">To</th>
              <th scope="col">Channel</th>
              <th scope="col">Port</th>
              <th scope="col" className="gateway-count">
                Reported by<span className="heading-note">gateways</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {packets.map((packet) => (
              <PacketTableRow key={packet.packet_row_id} packet={packet} />
            ))}
          </tbody>
        </table>
      </div>
      {paging.loadMoreError !== null ? (
        <ErrorPanel message={paging.loadMoreError} />
      ) : null}
      {nextBeforeId !== null ? (
        <button
          type="button"
          className="secondary-button page-button"
          disabled={paging.loadingMore}
          onClick={paging.loadMore}
        >
          {paging.loadingMore ? "Loading…" : "Load more packets"}
        </button>
      ) : (
        <p className="end-note">All saved packets shown.</p>
      )}
    </>
  );
}

function PacketTableRow({ packet }: { packet: PacketRow }) {
  const href = `#packets/${packet.packet_row_id}`;
  function openRow(event: MouseEvent) {
    // Links in the row, and selecting text to copy it, keep their own behaviour.
    if (
      (event.target as Element).closest("a") !== null ||
      window.getSelection()?.toString()
    ) {
      return;
    }
    window.location.hash = href;
  }
  return (
    <tr className="clickable-row" onClick={openRow}>
      <td>
        <ShortTime value={packet.first_received_at} />
      </td>
      <th scope="row">
        <a className="text-button mono" href={href}>
          {packet.packet_id}
        </a>
      </th>
      <td>
        <NodeLink nodeId={packet.sender_node_id} name={packet.sender_name} />
        <SenderRadioLine radio={packet.sender_radio} />
      </td>
      <td>
        <NodeLink
          nodeId={packet.recipient_node_id}
          name={packet.recipient_name}
        />
      </td>
      <td>{packet.channel}</td>
      <td
        className="port-cell"
        style={
          { "--port-color": portGroup(packet.portnum).color } as CSSProperties
        }
      >
        {packet.portnum ?? "Unknown"}
      </td>
      <td className="gateway-count">{packet.reception_count}</td>
    </tr>
  );
}
