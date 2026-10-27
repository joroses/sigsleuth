import { Suspense, lazy, useEffect, useState } from "react";

import { startMapRequests } from "./mapTimeFilter";
import { readView, useRememberedScroll, writeView } from "./remember";
import { routeFromHash } from "./routes";
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
const LazyRelayChecks = lazy(() =>
  import("./RelayChecks").then((module) => ({ default: module.RelayChecks })),
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

export function App() {
  const [route, setRoute] = useState(() => {
    const opened = routeFromHash(window.location.hash);
    // Before React even shows "Loading…" for the map's code.
    if (opened.page === "map") startMapRequests(opened.view);
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
        startMapRequests(next.view);
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
              : route.page === "relayChecks"
                ? "diagnostics"
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
            <LazyMapView
              key={JSON.stringify([route.nodeId, route.view])}
              nodeId={route.nodeId}
              linkView={route.view}
            />
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
              kind={route.kind}
              explorer={route.explorer}
            />
          ) : route.page === "packet" ? (
            <LazyPacketDetail
              key={JSON.stringify([
                route.id,
                route.hop,
                route.relays,
                route.receptionId,
                route.view,
              ])}
              packetRowId={route.id}
              hop={route.hop}
              relaysOnOpen={route.relays}
              receptionId={route.receptionId}
              linkView={route.view}
              openedFrom={openedFrom === null ? null : pageLabel(openedFrom)}
            />
          ) : route.page === "relayChecks" ? (
            <LazyRelayChecks receptionId={route.receptionId} />
          ) : route.page === "node" ? (
            <LazyNodeDetail
              key={JSON.stringify([route.id, route.view])}
              nodeId={route.id}
              linkView={route.view}
            />
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
