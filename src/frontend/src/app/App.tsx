import { Suspense, lazy, useEffect, useState } from "react";

import type { LinksTab } from "./LinksView";
import { startMapRequests } from "./mapTimeFilter";
import type { DirectedHop } from "./minimapMarkers";
import { readView, useRememberedScroll, writeView } from "./remember";
import { ApplicationBar, StatusPanel, mainPages } from "./ui";

const LazyMapView = lazy(() =>
  import("./MapView").then((module) => ({ default: module.MapView })),
);
const LazyZones = lazy(() =>
  import("./ZonesView").then((module) => ({ default: module.ZonesView })),
);
const LazyChat = lazy(() =>
  import("./ChatView").then((module) => ({ default: module.ChatView })),
);
const LazyNodes = lazy(() =>
  import("./NodesView").then((module) => ({ default: module.NodesView })),
);
const LazyLinks = lazy(() =>
  import("./LinksView").then((module) => ({ default: module.LinksView })),
);
const LazyDiagnostics = lazy(() =>
  import("./Diagnostics").then((module) => ({ default: module.Diagnostics })),
);
const LazyPacketDetail = lazy(() =>
  import("./PacketDetailView").then((module) => ({
    default: module.PacketDetailView,
  })),
);
const LazyNodeDetail = lazy(() =>
  import("./NodeDetailView").then((module) => ({
    default: module.NodeDetailView,
  })),
);

type Route =
  | { page: "map"; nodeId: number | null }
  | { page: "zones" }
  | { page: "chat" }
  | { page: "nodes" }
  | {
      page: "links";
      tab: LinksTab;
      pair: [number, number] | null;
      node: number | null;
      at: number | null;
    }
  | {
      page: "diagnostics";
      sender: number | null;
      heardDirectlyBy: number | null;
    }
  | { page: "packet"; id: number; hop: DirectedHop | null }
  | { page: "node"; id: number };

export function App() {
  const [route, setRoute] = useState(() => {
    const opened = routeFromHash(window.location.hash);
    // Before React even shows "Loading…" for the map's code.
    if (opened.page === "map") startMapRequests();
    return opened;
  });
  // Counts the history entries shown, so each one gets its scroll back.
  const [entry, setEntry] = useState(0);
  const content = useRememberedScroll(String(entry));
  // The page this history entry was opened from, if it was opened by
  // following a link here; the entry before it in the history.
  const [openedFrom, setOpenedFrom] = useState(() => {
    if (readView(OPENED_FROM) === undefined) {
      writeView(OPENED_FROM, null);
    }
    return readView(OPENED_FROM) as string | null;
  });

  useEffect(() => {
    const syncHash = (event: HashChangeEvent) => {
      // A new entry keeps the page it came from; Back and Forward return to
      // entries that already know theirs.
      if (readView(OPENED_FROM) === undefined) {
        writeView(OPENED_FROM, new URL(event.oldURL).hash);
      }
      setOpenedFrom(readView(OPENED_FROM) as string | null);
      const next = routeFromHash(window.location.hash);
      // Before the map mounts, so it finds its requests on their way; moving
      // within the map keeps the window it already loaded.
      if (
        next.page === "map" &&
        routeFromHash(new URL(event.oldURL).hash).page !== "map"
      ) {
        startMapRequests();
      }
      setRoute(next);
      setEntry((count) => count + 1);
    };
    window.addEventListener("hashchange", syncHash);
    return () => window.removeEventListener("hashchange", syncHash);
  }, []);

  return (
    <div className="app-shell">
      <ApplicationBar
        activePage={
          route.page === "node"
            ? "nodes"
            : route.page === "packet"
              ? null
              : route.page
        }
      />
      <main
        ref={content}
        className={`app-content ${route.page === "map" ? "page-map" : route.page === "chat" ? "page-chat" : "page-document"}`}
      >
        {/* Keyed by page: while the next page's code loads, Suspense would
            otherwise keep the old page mounted but hidden, and a quick click
            back to it would revive it with its old selection. */}
        <Suspense
          key={route.page}
          fallback={<StatusPanel>Loading…</StatusPanel>}
        >
          {route.page === "map" ? (
            <LazyMapView key={route.nodeId} nodeId={route.nodeId} />
          ) : route.page === "zones" ? (
            <LazyZones />
          ) : route.page === "chat" ? (
            <LazyChat />
          ) : route.page === "nodes" ? (
            <LazyNodes />
          ) : route.page === "links" ? (
            <LazyLinks
              tab={route.tab}
              pair={route.pair}
              node={route.node}
              at={route.at}
            />
          ) : route.page === "packet" ? (
            <LazyPacketDetail
              key={`${route.id}-${route.hop?.fromNodeId}-${route.hop?.toNodeId}`}
              packetRowId={route.id}
              hop={route.hop}
              openedFrom={openedFrom === null ? null : pageLabel(openedFrom)}
            />
          ) : route.page === "node" ? (
            <LazyNodeDetail key={route.id} nodeId={route.id} />
          ) : (
            <LazyDiagnostics
              sender={route.sender}
              heardDirectlyBy={route.heardDirectlyBy}
            />
          )}
        </Suspense>
      </main>
    </div>
  );
}

const OPENED_FROM = "openedFrom";

/** A page's link and name, for a link back to it. */
function pageLabel(hash: string): { href: string; label: string } {
  const page = routeFromHash(hash).page;
  const label =
    page === "node"
      ? "node"
      : page === "packet"
        ? "packet"
        : (mainPages.find((main) => main.page === page)?.label ??
          "previous page");
  return { href: hash || "#map", label };
}

function routeFromHash(hash: string): Route {
  // #health is the page's old name.
  if (hash === "#zones" || hash === "#health") {
    return { page: "zones" };
  }
  if (hash === "#chat") {
    return { page: "chat" };
  }
  if (hash === "#nodes") {
    return { page: "nodes" };
  }
  // The links or the explorer, optionally around one node; the explorer,
  // optionally only runs with a hop between two nodes; or the line of sight,
  // optionally between two nodes, where they were at a time in ms.
  const linksTab = hash.match(
    /^#links(?:\/(traceroutes|line-of-sight))?(?:\/([1-9]\d*)(?:-([1-9]\d*))?)?(?:@(\d+))?$/,
  );
  if (linksTab !== null) {
    const [, tab = "links", first, second, time] = linksTab;
    const pair: [number, number] | null =
      second === undefined || tab === "links"
        ? null
        : [Number(first), Number(second)];
    const node =
      first === undefined || second !== undefined || tab === "line-of-sight"
        ? null
        : Number(first);
    const at =
      time === undefined || pair === null || tab !== "line-of-sight"
        ? null
        : Number(time);
    return { page: "links", tab: tab as LinksTab, pair, node, at };
  }
  // Diagnostics, optionally listing the packets one node sent, optionally
  // only those a second heard directly.
  const diagnostics = hash.match(
    /^#diagnostics(?:\/packets\/([1-9]\d*)(?:-([1-9]\d*))?)?$/,
  );
  if (diagnostics !== null) {
    const [, first, second] = diagnostics;
    const ids = [first, second].map((id) =>
      id === undefined || Number(id) > 0xffffffff ? null : Number(id),
    );
    return {
      page: "diagnostics",
      sender: ids[0],
      heardDirectlyBy: ids[0] !== null ? ids[1] : null,
    };
  }
  // A packet, optionally showing only the reports where the first node
  // was heard by the second.
  const packet = hash.match(
    /^#packets\/([1-9]\d*)(?:\/hop\/([1-9]\d*)-([1-9]\d*))?$/,
  );
  if (packet !== null && Number.isSafeInteger(Number(packet[1]))) {
    const hop =
      packet[2] === undefined
        ? null
        : {
            fromNodeId: Number(packet[2]),
            toNodeId: Number(packet[3]),
          };
    return { page: "packet", id: Number(packet[1]), hop };
  }
  const node = hash.match(/^#nodes\/([1-9]\d*)$/);
  if (node !== null && Number(node[1]) <= 0xffffffff) {
    return { page: "node", id: Number(node[1]) };
  }
  const mapNode = hash.match(/^#map\/([1-9]\d*)$/);
  if (mapNode !== null && Number(mapNode[1]) <= 0xffffffff) {
    return { page: "map", nodeId: Number(mapNode[1]) };
  }
  return { page: "map", nodeId: null };
}
