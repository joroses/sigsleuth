import { type LinkKind, mapLinksUrl, mapNodesUrl, textSendersUrl } from "./api";
import { DEFAULT_LAYER_CHOICE } from "./mapLinkLayers";
import { readView, savedSetting, writeView } from "./remember";
import { openedAt, startEarly } from "./useApi";

const HOUR_MS = 60 * 60 * 1000;
// Positions older than this are hidden unless the window is "All".
const POSITION_MAX_AGE_HOURS = 7 * 24;

/** How recently a node must have been seen, and links observed; null is all history. */
export const SEEN_WINDOWS = [
  { id: "1h", label: "1 hour", hours: 1 },
  { id: "6h", label: "6 hours", hours: 6 },
  { id: "24h", label: "24 hours", hours: 24 },
  { id: "3d", label: "3 days", hours: 3 * 24 },
  { id: "7d", label: "7 days", hours: 7 * 24 },
  { id: "all", label: "All", hours: null },
] as const;

export type SeenWindow = (typeof SEEN_WINDOWS)[number]["id"];

export const DEFAULT_SEEN_WINDOW: SeenWindow = "3d";

export function isSeenWindow(value: unknown): boolean {
  return SEEN_WINDOWS.some((option) => option.id === value);
}

/** The window the map shows and when it was chosen, kept in its history entry. */
export const SHOWN_WINDOW_VIEW = "map.window";
export interface ShownWindow {
  window: SeenWindow;
  chosenAt: number;
}

/**
 * A window as chosen now, or when it was last chosen if that was minutes ago
 * (openedAt), so a new visit soon after shows what was loaded for it.
 */
export function mapWindowStart(window: SeenWindow): ShownWindow {
  return { window, chosenAt: openedAt(`map.${window}`) };
}

export interface MapUrls {
  /** The window's start; undefined for all history. */
  since: string | undefined;
  nodes: string;
  traceroute: string;
  direct: string;
  neighbor: string;
  textSenders: string;
}

/** The map's API URLs for a window ending now; nodes and links share its start. */
export function mapUrls(window: SeenWindow, now: number): MapUrls {
  const hours = SEEN_WINDOWS.find((option) => option.id === window)?.hours;
  const ago = (hours: number) => new Date(now - hours * HOUR_MS).toISOString();
  const since = hours == null ? undefined : ago(hours);
  const links = (linkKind: LinkKind) => mapLinksUrl(linkKind, since);
  return {
    since,
    nodes: mapNodesUrl({
      seenSince: since,
      positionSince: hours == null ? undefined : ago(POSITION_MAX_AGE_HOURS),
    }),
    traceroute: links("traceroute"),
    direct: links("direct"),
    neighbor: links("neighbor"),
    textSenders: textSendersUrl(since),
  };
}

/**
 * Starts the map's requests when the app opens on it, while the map's code
 * and MapLibre still load: the window shown, kept in this history entry (and
 * kept there now for a new one), and the link layers chosen, as MapView will
 * read them, so it finds its requests already on their way (useApi's
 * startEarly).
 */
export function startMapRequests(): void {
  let shown = readView(SHOWN_WINDOW_VIEW) as ShownWindow | undefined;
  if (shown === undefined) {
    shown = mapWindowStart(
      savedSetting<SeenWindow>("seenWindow", DEFAULT_SEEN_WINDOW, isSeenWindow),
    );
    writeView(SHOWN_WINDOW_VIEW, shown);
  }
  const urls = mapUrls(shown.window, shown.chosenAt);
  startEarly(urls.nodes, shown.chosenAt);
  startEarly(urls.textSenders, shown.chosenAt);
  const layers = savedSetting("linkLayers", DEFAULT_LAYER_CHOICE);
  for (const kind of ["traceroute", "direct", "neighbor"] as const) {
    if (layers[kind]) startEarly(urls[kind], shown.chosenAt);
  }
}
