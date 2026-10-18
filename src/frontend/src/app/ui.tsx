import {
  ButtonHTMLAttributes,
  ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";

import type { LinkDirection, NodeSummary, PacketReception } from "./api";
import { fadeMargin, linkTier, linkTierColor, marginTier } from "./linkQuality";
import { loadSetting } from "./remember";
import {
  BROADCAST_NODE_ID,
  formatNodeId,
  nodeHref,
  nodeName,
  relayByte,
  relayMaybeTitle,
  relayNodeTitle,
  shortLabel,
} from "./nodeDisplay";

export type MainPage =
  "map" | "zones" | "chat" | "nodes" | "links" | "diagnostics";

export const mainPages: { page: MainPage; href: string; label: string }[] = [
  { page: "map", href: "#map", label: "Map" },
  { page: "zones", href: "#zones", label: "Zones" },
  { page: "chat", href: "#chat", label: "Chat" },
  { page: "nodes", href: "#nodes", label: "Nodes" },
  { page: "links", href: "#links", label: "Links" },
  // Linked only in a browser where it has loaded (DIAGNOSTICS_LINKED): its
  // data takes the admin password, so the operator first opens it by typing
  // #diagnostics.
  { page: "diagnostics", href: "#diagnostics", label: "Diagnostics" },
];

/** The setting Diagnostics saves once it has loaded in this browser. */
export const DIAGNOSTICS_LINKED = "diagnostics.linked";

// The AGPL asks a modified copy run for others to offer its own source here.
const SOURCE_URL = "https://github.com/joroses/sigsleuth";

type IconName =
  | MainPage
  | "back"
  | "check"
  | "chevron"
  | "close"
  | "collapse"
  | "contact"
  | "copy"
  | "coverage"
  | "expand"
  | "external"
  | "open"
  | "filter"
  | "layers"
  | "locate"
  | "mute"
  | "pin"
  | "profile"
  | "radio"
  | "route";

const iconPaths: Record<IconName, ReactNode> = {
  map: (
    <>
      <path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2Z" />
      <path d="M9 3v16M15 5v16" />
    </>
  ),
  // Two overlapping areas.
  zones: (
    <>
      <circle cx="8.5" cy="10" r="6" />
      <circle cx="15.5" cy="14" r="6" />
    </>
  ),
  chat: <path d="M4 5h16v11H9l-5 4Z" />,
  nodes: (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="18" cy="8" r="2.5" />
      <circle cx="10" cy="18" r="2.5" />
      <path d="m8.4 6.5 7.2 1M16.6 10 11.4 16M7 8.3l2.2 7.4" />
    </>
  ),
  links: (
    <>
      <circle cx="5" cy="17" r="2.5" />
      <circle cx="19" cy="7" r="2.5" />
      <path d="M7.2 15.6 16.8 8.4" />
      <path d="M4 9a8 8 0 0 1 6-6M20 15a8 8 0 0 1-6 6" />
    </>
  ),
  diagnostics: (
    <>
      <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12Z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  // Back to the page this one was opened from.
  back: <path d="M19 12H6M11 6l-6 6 6 6" />,
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  // Points right; turned down on an expanded row's button.
  chevron: <path d="m9 6 6 6-6 6" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  // A person with a plus: add a node to a Meshtastic app.
  contact: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M3 20a6 6 0 0 1 12 0M19 8v6M16 11h6" />
    </>
  ),
  copy: (
    <>
      <rect x="8" y="8" width="12" height="12" rx="2" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </>
  ),
  // The coverage grid: survey cells, some filled.
  coverage: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 3v18M15 3v18M3 9h18M3 15h18" />
      <path d="M4 4h4v4H4ZM10 10h4v4h-4ZM16 4h4v4h-4Z" fill="currentColor" />
    </>
  ),
  collapse: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M15 4v16m-6-11 3 3-3 3" />
    </>
  ),
  expand: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M15 4v16m-4-11-3 3 3 3" />
    </>
  ),
  // Out of SigSleuth, to another site or app.
  external: (
    <>
      <path d="M14 4h6v6M20 4l-9 9" />
      <path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4" />
    </>
  ),
  // An arrow into another page of SigSleuth, in this tab. (An "external"
  // box-and-arrow would suggest a new tab or another site.)
  open: <path d="M5 12h13M13 6l6 6-6 6" />,
  filter: <path d="M3 5h18l-7 8v6l-4 2v-8Z" />,
  layers: (
    <>
      <path d="m12 3 9 5-9 5-9-5Z" />
      <path d="m3 12 9 5 9-5M3 16l9 5 9-5" />
    </>
  ),
  locate: (
    <>
      <circle cx="12" cy="12" r="6" />
      <circle cx="12" cy="12" r="1" />
      <path d="M12 2v4m0 12v4M2 12h4m12 0h4" />
    </>
  ),
  // A speaker crossed out: hide a sender's messages.
  mute: (
    <>
      <path d="M4 9h4l5-4v14l-5-4H4Z" />
      <path d="m16 9 5 6M21 9l-5 6" />
    </>
  ),
  pin: (
    <>
      <path d="M12 21s-6-5.5-6-11a6 6 0 0 1 12 0c0 5.5-6 11-6 11Z" />
      <circle cx="12" cy="10" r="2" />
    </>
  ),
  // Terrain under a dashed sight line: the elevation profile of a link.
  profile: (
    <>
      <path d="M3 20v-4l4-4 3 2 4-5 3 3 4-2v10Z" />
      <path d="M3 5l18 2" strokeDasharray="2 2.5" />
    </>
  ),
  // A route through relays: hops from one end to the other.
  route: (
    <>
      <circle cx="4" cy="18" r="2" />
      <circle cx="10" cy="7" r="2" />
      <circle cx="15" cy="16" r="2" />
      <circle cx="20" cy="5" r="2" />
      <path d="m5 16.3 4-7.6M11 8.8l3 5.4M16 14.2l3-7.4" />
    </>
  ),
  radio: (
    <>
      <circle cx="12" cy="12" r="2" />
      <path d="m9 21 3-7 3 7M7 7a7 7 0 0 0 0 10M17 7a7 7 0 0 1 0 10M4 4a11 11 0 0 0 0 16M20 4a11 11 0 0 1 0 16" />
    </>
  ),
};

export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="icon"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {iconPaths[name]}
    </svg>
  );
}

// GitHub's own mark (Octicons, MIT), which GitHub allows for linking to a
// project there. It is a filled shape, unlike the stroked icons above.
function GitHubMark() {
  return (
    <svg
      className="icon"
      width="20"
      height="20"
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z" />
    </svg>
  );
}

/** The instance name the server puts in the page (SIGSLEUTH_NAME), if any. */
function instanceName(): string | null {
  return (
    document
      .querySelector('meta[name="sigsleuth-name"]')
      ?.getAttribute("content") ?? null
  );
}

export function ApplicationBar({
  activePage,
}: {
  activePage: MainPage | null;
}) {
  const name = instanceName();
  // Read on each page change, so the tab appears after the first visit.
  const pages = mainPages.filter(
    ({ page }) =>
      page !== "diagnostics" ||
      activePage === "diagnostics" ||
      loadSetting(DIAGNOSTICS_LINKED) === true,
  );
  const navRef = useRef<HTMLElement>(null);
  // On a phone the tabs scroll sideways; keep the current page's tab in view.
  useEffect(() => {
    navRef.current
      ?.querySelector('[aria-current="page"]')
      ?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [activePage]);
  return (
    <header className="application-bar">
      <a className="brand" href="#map">
        <span className="brand-symbol">
          <Icon name="radio" />
        </span>
        <span className="brand-text">SigSleuth</span>
        {name && <span className="instance-name">{name}</span>}
      </a>
      <nav aria-label="Main pages" ref={navRef}>
        {pages.map(({ page, href, label }) => (
          <a
            key={page}
            href={href}
            aria-current={activePage === page ? "page" : undefined}
          >
            <Icon name={page} />
            {label}
          </a>
        ))}
      </nav>
      <a
        className="source-link"
        href={SOURCE_URL}
        aria-label="Source code on GitHub"
        title="Source code on GitHub"
      >
        <GitHubMark />
      </a>
    </header>
  );
}

/**
 * A link back to the page this one was opened from. Following it goes back in
 * the browser history, so that page returns with its scroll and selection.
 */
export function ReturnLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      className="return-link"
      href={href}
      onClick={(event) => {
        if (
          event.button === 0 &&
          !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
        ) {
          event.preventDefault();
          window.history.back();
        }
      }}
    >
      <Icon name="back" />
      {label}
    </a>
  );
}

/** A button back to the view this one was opened from, with its name. */
export function BackButton({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <button type="button" className="return-link" onClick={onClick}>
      <Icon name="back" />
      {label}
    </button>
  );
}

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: "neutral" | "green" | "blue" | "amber" | "red";
  title?: string;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

/**
 * Beside a distance that is probably wrong, with why in its tooltip: an end
 * the backend doubts (links.doubtful_positions), named in suspects, or a link
 * the terrain check finds unlikely (links.unlikely_links). Pages rank these
 * last by km. Nothing when neither applies.
 */
export function DoubtfulBadge({
  suspects,
  unlikely = false,
}: {
  /** The names of the doubted nodes. */
  suspects: readonly string[];
  unlikely?: boolean;
}) {
  if (suspects.length === 0 && !unlikely) return null;
  const reasons = [];
  if (suspects.length === 1) {
    reasons.push(
      `${suspects[0]} is probably not where it says: its own direct links are far from its position. It may have moved, typed a wrong position, or share its node number with another device.`,
    );
  } else if (suspects.length > 1) {
    reasons.push(
      `${joinNames(suspects)} are probably not where they say: their own direct links are far from their positions.`,
    );
  }
  if (unlikely) {
    reasons.push(
      "Too far for radio over this terrain: an end's position is probably wrong.",
    );
  }
  return (
    <Badge tone="amber" title={`Doubtful distance. ${reasons.join(" ")}`}>
      doubtful
    </Badge>
  );
}

function joinNames(names: readonly string[]): string {
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * Beside a distance with a node heard in the air in the window
 * (links.airborne_nodes): its links are radio, but measured from where it
 * was on the ground. Pages rank these last by km.
 */
export function AirborneBadge() {
  return (
    <Badge
      tone="blue"
      title="A node here was airborne; distances are from the ground"
    >
      ✈ aircraft
    </Badge>
  );
}

/**
 * Beside a long distance not confirmed yet (links.PENDING_KM): an end has not
 * reported its position since it was heard, or moved since. Pages rank these
 * last by km.
 */
export function PendingBadge() {
  return (
    <Badge
      tone="neutral"
      title="Pending: not confirmed until both ends report their position again."
    >
      pending
    </Badge>
  );
}

/** A line's sample in a legend: solid, or dashed with `dashes`. */
export function LegendLine({ dashes }: { dashes: string | undefined }) {
  return (
    <svg width="24" height="10" aria-hidden="true">
      <line
        x1="1"
        y1="5"
        x2="23"
        y2="5"
        stroke="currentColor"
        strokeWidth={2}
        strokeDasharray={dashes}
      />
    </svg>
  );
}

export function IconButton({
  icon,
  label,
  ...props
}: {
  icon: IconName;
  label: string;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      type="button"
      className="icon-button"
      aria-label={label}
      title={label}
    >
      <Icon name={icon} />
    </button>
  );
}

export function CopyValue({ value, label }: { value: string; label: string }) {
  const [result, setResult] = useState<{
    value: string;
    copied: boolean;
  } | null>(null);
  const feedback =
    result?.value === value
      ? result.copied
        ? `Copied ${label}`
        : `Could not copy ${label}. Select the text to copy it.`
      : "";

  useEffect(() => {
    if (result === null) return;
    const timer = window.setTimeout(() => setResult(null), 2000);
    return () => window.clearTimeout(timer);
  }, [result]);

  async function copy() {
    let copied: boolean;
    try {
      await navigator.clipboard.writeText(value);
      copied = true;
    } catch {
      // The clipboard API needs HTTPS; plain HTTP uses a temporary selection.
      const field = document.createElement("textarea");
      const focused = document.activeElement;
      field.value = value;
      field.readOnly = true;
      field.style.position = "fixed";
      field.style.top = "0";
      field.style.opacity = "0";
      document.body.append(field);
      field.select();
      try {
        copied = document.execCommand("copy");
      } catch {
        copied = false;
      } finally {
        field.remove();
        if (focused instanceof HTMLElement)
          focused.focus({ preventScroll: true });
      }
    }
    setResult({ value, copied });
  }

  return (
    <span className="copy-value">
      <span className="mono">{value}</span>
      <IconButton
        icon={result?.value === value && result.copied ? "check" : "copy"}
        label={feedback || `Copy ${label}`}
        onClick={() => void copy()}
      />
      <span className="visually-hidden" role="status">
        {feedback}
      </span>
    </span>
  );
}

export function StatusPanel({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "error";
}) {
  return (
    <div
      className={`status-panel ${tone === "error" ? "error-panel" : ""}`}
      role={tone === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}

/** A compact error under content that is still shown, such as a failed "load more". */
export function ErrorPanel({ message }: { message: string }) {
  return (
    <p className="error-panel compact" role="alert">
      {message}
    </p>
  );
}

export function DataItem({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/**
 * A figure for each direction of a link or zone pair around ⇄: the left one
 * is the row's first end to its second, the right one the reverse.
 */
export function BothWays({
  left,
  right,
}: {
  left: ReactNode;
  right: ReactNode;
}) {
  return (
    <span className="both-ways">
      {left}
      <span className="muted" aria-hidden="true">
        ⇄
      </span>
      {right}
    </span>
  );
}

/** A fade margin in dB, signed, after a dot in its tier colour. */
export function MarginValue({ value }: { value: number }) {
  return (
    <span className="margin-value">
      <TierDot color={linkTierColor(marginTier(value))} />
      {value > 0 ? "+" : ""}
      {value.toFixed(1)} dB
    </span>
  );
}

/** The arrow between a link's two nodes, pointing from sender to hearer. */
export function LinkArrow() {
  return (
    <svg className="link-arrow" viewBox="0 0 24 12" role="img" aria-label="to">
      <path d="M0 3.5h14V0l10 6-10 6V8.5H0z" />
    </svg>
  );
}

/** A small round swatch in a tier's colour, before the figure it rates. */
export function TierDot({ color }: { color: string }) {
  return (
    <span
      className="tier-dot"
      style={{ background: color }}
      aria-hidden="true"
    />
  );
}

/**
 * One direction of a link: its average SNR with the map's tier colour for its
 * fade margin, or "—" when it was not heard that way or had no SNR. The
 * tooltip starts with `label` (who heard whom) when given.
 */
export function DirectionSnr({
  direction,
  spreadingFactor,
  label,
}: {
  direction: LinkDirection;
  spreadingFactor: number;
  label?: string;
}) {
  const prefix = label === undefined ? "" : `${label}: `;
  const snr = direction.average_snr;
  const margin = fadeMargin(snr, spreadingFactor);
  if (snr === null || margin === null) {
    return (
      <span
        className="muted"
        title={`${prefix}${direction.observations === 0 ? "not heard this way" : "no SNR reported"}`}
      >
        —
      </span>
    );
  }
  return (
    <span title={`${prefix}${margin.toFixed(1)} dB margin`}>
      <TierDot color={linkTierColor(linkTier(snr, spreadingFactor))} />
      {snr.toFixed(1)}
    </span>
  );
}

/**
 * One hop's SNR after a dot in its tier colour at spreadingFactor, which is
 * left out without one. Exactly 0 dB reads "MQTT?" and is not rated: it is
 * usually a node that got the packet from MQTT, not a radio hop, and the
 * backend leaves it out of links (MQTT_HOP_SNR_DB). digits fixes the
 * decimals; without it the SNR shows as reported.
 */
export function HopSnr({
  className,
  snr,
  spreadingFactor,
  digits,
  after = null,
}: {
  className?: string;
  snr: number | null;
  spreadingFactor?: number;
  digits?: number;
  after?: string | null;
}) {
  const mqtt = snr === 0;
  const margin =
    mqtt || spreadingFactor === undefined
      ? null
      : fadeMargin(snr, spreadingFactor);
  return (
    <span
      className={className}
      title={
        mqtt
          ? "0 dB: probably crossed MQTT, not a radio hop"
          : margin === null
            ? undefined
            : `${margin.toFixed(1)} dB margin`
      }
    >
      {margin === null ? null : (
        <TierDot color={linkTierColor(marginTier(margin))} />
      )}
      {snr === null
        ? "? dB"
        : mqtt
          ? "MQTT?"
          : `${digits === undefined ? snr : snr.toFixed(digits)} dB`}
      {after}
    </span>
  );
}

/**
 * A reception's relay byte, with the node it names below it: marked "≈"
 * unless the gateway heard that node directly. A byte naming none lists the
 * nodes it may be in its tooltip.
 */
export function RelayCell({ reception }: { reception: PacketReception }) {
  const relay = reception.relay_node;
  const byte = relayByte(reception);
  return (
    <td className="mono">
      {relay === null && byte !== "—" ? (
        <span title={relayMaybeTitle(reception)}>{byte}</span>
      ) : (
        byte
      )}
      {relay === null ? null : (
        <>
          <br />
          <a
            className="relay-guess"
            href={nodeHref(relay.node_id)}
            title={relayNodeTitle(relay)}
          >
            {relay.how === "heard_directly" ? "" : "≈ "}
            {relay.name ?? shortLabel(relay.node_id, null)}
          </a>
        </>
      )}
    </td>
  );
}

export function Timestamp({ value }: { value: string }) {
  return <time dateTime={value}>{new Date(value).toLocaleString()}</time>;
}

const shortTimeFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** A date and time to the minute, with the full time on hover. */
export function ShortTime({ value }: { value: string }) {
  const time = new Date(value);
  return (
    <time dateTime={value} title={time.toLocaleString()}>
      {shortTimeFormat.format(time)}
    </time>
  );
}

/**
 * When a view was opened (useApi's openedAt), which a new visit soon after
 * shows again, and a button to load it afresh.
 */
export function OpenedAt({
  at,
  onRefresh,
}: {
  at: number;
  onRefresh: () => void;
}) {
  return (
    <span className="opened-at">
      As of <ShortTime value={new Date(at).toISOString()} />
      <button type="button" className="secondary-button" onClick={onRefresh}>
        Refresh
      </button>
    </span>
  );
}

/** A link to a node's page; broadcast means every node, so it has no page. */
export function NodeLink({
  nodeId,
  name = null,
  title = formatNodeId(nodeId),
}: {
  nodeId: number;
  name?: string | null;
  title?: string;
}) {
  if (nodeId === BROADCAST_NODE_ID) {
    return <span className="muted">Broadcast</span>;
  }
  return (
    <a className="text-button" href={nodeHref(nodeId)} title={title}>
      {name ?? formatNodeId(nodeId)}
    </a>
  );
}

/** A node's link, named from a page's nodes when it is among them. */
export function NodeName({
  nodeId,
  nodes,
}: {
  nodeId: number;
  nodes: ReadonlyMap<number, NodeSummary>;
}) {
  const node = nodes.get(nodeId);
  const name = nodeName(node);
  // The full name stays on hover when a long one is cut short in the table.
  return (
    <NodeLink
      nodeId={nodeId}
      name={name}
      title={name === null ? undefined : `${name} (${formatNodeId(nodeId)})`}
    />
  );
}

/**
 * A table wider than the page scrolls sideways with a scrollbar pinned to the
 * bottom of the window, so it stays reachable while the page scrolls down.
 */
export function WideTable({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const bar = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);

  useEffect(() => {
    const element = wrap.current;
    if (element === null || typeof ResizeObserver === "undefined") return;
    const measure = () =>
      setWidth(
        element.scrollWidth > element.clientWidth ? element.scrollWidth : null,
      );
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => observer.disconnect();
  }, []);

  const follow = (from: HTMLDivElement | null, to: HTMLDivElement | null) => {
    if (from && to && to.scrollLeft !== from.scrollLeft)
      to.scrollLeft = from.scrollLeft;
  };

  return (
    <>
      <div
        className={`table-wrap wide-table ${className}`}
        ref={wrap}
        onScroll={() => follow(wrap.current, bar.current)}
      >
        {children}
      </div>
      {width !== null ? (
        <div
          className="wide-table-scrollbar"
          ref={bar}
          onScroll={() => follow(bar.current, wrap.current)}
        >
          <div style={{ width }} />
        </div>
      ) : null}
    </>
  );
}

/**
 * A column heading that sorts its table when clicked: highest first, or
 * lowest first when the table passes descending={false} for the active column.
 */
export function SortHeading<S extends string>({
  label,
  note,
  title,
  sort,
  active,
  descending = true,
  onSort,
}: {
  label: string;
  note?: string;
  title?: string;
  sort: S;
  active: S;
  descending?: boolean;
  onSort: (sort: S) => void;
}) {
  const isActive = active === sort;
  return (
    <th
      scope="col"
      title={title}
      aria-sort={
        isActive ? (descending ? "descending" : "ascending") : undefined
      }
    >
      <button
        type="button"
        className={`sort-button ${isActive ? "is-active" : ""}`}
        onClick={() => onSort(sort)}
      >
        {label}{" "}
        <span className="sort-arrow" aria-hidden="true">
          {isActive && !descending ? "↑" : "↓"}
        </span>
      </button>
      {note ? <span className="heading-note">{note}</span> : null}
    </th>
  );
}
