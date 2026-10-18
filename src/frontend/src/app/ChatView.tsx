import {
  MouseEvent,
  ReactNode,
  RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  ChatChannelList,
  ChatFilter,
  ChatMessage,
  ChatPage,
  PacketDetail,
  chatChannelsUrl,
  chatUrl,
  request,
  packetUrl,
} from "./api";
import { ChatLine, ChatThread, chatThreads } from "./chatThreads";
import {
  ChatWindow,
  MESSAGE_BUDGET,
  startWindow,
  withNewer,
} from "./chatWindow";
import {
  formatKm,
  formatNodeId,
  hopsUsed,
  longLabel,
  nodeHref,
} from "./nodeDisplay";
import { useSetting, useViewState } from "./remember";
import { useApi, withBeforeId } from "./useApi";
import { ErrorPanel, Icon, RelayCell, StatusPanel } from "./ui";

// Replies shown under a message before the rest collapse behind a button.
const VISIBLE_REPLIES = 3;
// Sender name colours, picked by node number so a sender keeps its colour.
const SENDER_COLOURS = [
  "#7dcb9f",
  "#8dbcd8",
  "#e6b664",
  "#f29380",
  "#bda0e6",
  "#6fcfcf",
  "#e894ba",
  "#bccb72",
];

// How often the page asks for new messages while it is visible.
const POLL_INTERVAL_MS = 10_000;
// Within this many pixels of the end counts as reading the newest messages.
const BOTTOM_SLACK_PX = 40;
// Channels shown as chips, busiest first; the rest go in the dropdown.
const CHANNEL_CHIPS = 5;
// The search runs once typing pauses this long.
const SEARCH_DELAY_MS = 300;

type DetailLoader = (packetRowId: number) => Promise<PacketDetail>;

/** A sender whose messages are hidden; the name is kept to list it. */
type MutedSender = { node_id: number; name: string };

function isMutedList(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.every(
      (item: Partial<MutedSender> | null) =>
        typeof item?.node_id === "number" && typeof item.name === "string",
    )
  );
}

export function ChatView() {
  const [channel, setChannel] = useSetting<string | null>(
    "chatChannel",
    null,
    (value) => value === null || typeof value === "string",
  );
  // Muted senders are kept in this browser's localStorage, like the channel.
  const [muted, setMuted] = useSetting<MutedSender[]>(
    "chatMuted",
    [],
    isMutedList,
  );
  const [typed, setTyped] = useViewState("chat.search", "");
  const [search, setSearch] = useViewState("chat.query", "");
  useEffect(() => {
    if (typed === search) return;
    const timer = window.setTimeout(() => setSearch(typed), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [typed, search, setSearch]);
  const filter = useMemo(
    () => ({ channel, search, hidden: muted.map((sender) => sender.node_id) }),
    [channel, search, muted],
  );
  const url = chatUrl(filter);
  const [atBottom, setAtBottom] = useState(true);
  const {
    messages,
    loaded,
    error,
    nextBeforeId,
    loadOlder,
    loadingOlder,
    loadOlderError,
  } = useChatMessages(url, filter, atBottom);
  const threads = useMemo(() => chatThreads(messages), [messages]);
  const loadDetail = useDetailCache();
  const mute = useCallback(
    (sender: MutedSender) =>
      setMuted((current) =>
        current.some((item) => item.node_id === sender.node_id)
          ? current
          : [...current, sender],
      ),
    [setMuted],
  );
  const { data: channels } = useApi<ChatChannelList>(chatChannelsUrl);

  const newestId = messages.at(-1)?.packet_row_id ?? 0;
  // The newest message the reader has scrolled down to; newer ones count in the pill.
  const [seenId, setSeenId] = useState(0);
  const unseen = atBottom
    ? 0
    : messages.filter((message) => message.packet_row_id > seenId).length;
  const scroller = useScrollAnchoring(url, messages, atBottom);

  function followScroll() {
    const element = scroller.current;
    if (element === null) return;
    const bottom =
      element.scrollHeight - element.scrollTop - element.clientHeight <
      BOTTOM_SLACK_PX;
    setAtBottom(bottom);
    if (bottom) setSeenId(newestId);
  }

  // The pill sits inside the scrolling column, so its parent is the scroller.
  function scrollToNewest(event: MouseEvent<HTMLButtonElement>) {
    const element = event.currentTarget.parentElement;
    if (element !== null) element.scrollTop = element.scrollHeight;
  }

  // The busiest channels get chips; the long tail of test and one-off
  // channels goes in a dropdown so it cannot crowd the toolbar.
  const channelList = channels?.channels ?? [];
  const chipChannels = channelList.slice(0, CHANNEL_CHIPS);
  const moreChannels = channelList.slice(CHANNEL_CHIPS);
  // A chosen channel without a chip shows in the dropdown, even one no longer
  // listed (remembered from an earlier visit).
  const chosenInMore =
    channel !== null && !chipChannels.some((item) => item.channel === channel)
      ? channel
      : null;
  const chosenUnlisted = !moreChannels.some(
    (item) => item.channel === chosenInMore,
  );
  return (
    <section className="chat-frame" aria-labelledby="chat-heading">
      <div className="chat-toolbar" role="toolbar" aria-label="Channel">
        <h2 id="chat-heading">Chat</h2>
        <ChannelChip
          label="All"
          pressed={channel === null}
          onClick={() => setChannel(null)}
        />
        {chipChannels.map((item) => (
          <ChannelChip
            key={item.channel}
            label={`${item.channel} ${item.messages}`}
            pressed={channel === item.channel}
            onClick={() => setChannel(item.channel)}
          />
        ))}
        {moreChannels.length > 0 || chosenInMore !== null ? (
          <select
            className="chat-channel-more"
            aria-label="More channels"
            data-active={chosenInMore !== null}
            value={chosenInMore ?? ""}
            onChange={(event) => setChannel(event.target.value)}
          >
            <option value="" disabled>
              More channels ({moreChannels.length})
            </option>
            {chosenInMore !== null && chosenUnlisted ? (
              <option value={chosenInMore}>{chosenInMore}</option>
            ) : null}
            {moreChannels.map((item) => (
              <option key={item.channel} value={item.channel}>
                {item.channel} · {item.messages}
              </option>
            ))}
          </select>
        ) : null}
        <input
          type="search"
          className="chat-search"
          aria-label="Search messages"
          placeholder="Search text or names"
          maxLength={100}
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
        />
        {muted.length > 0 ? (
          <MutedList
            muted={muted}
            onUnmute={(nodeId) =>
              setMuted(muted.filter((sender) => sender.node_id !== nodeId))
            }
          />
        ) : null}
      </div>
      <div className="chat-scroller" ref={scroller} onScroll={followScroll}>
        <div className="chat-column">
          {nextBeforeId !== null ? (
            <button
              type="button"
              className="secondary-button chat-older"
              disabled={loadingOlder}
              onClick={loadOlder}
            >
              {loadingOlder ? "Loading…" : "Load older"}
            </button>
          ) : null}
          {loadOlderError !== null ? (
            <ErrorPanel message={loadOlderError} />
          ) : null}
          {!loaded && error === null ? (
            <StatusPanel>Loading messages…</StatusPanel>
          ) : null}
          {error !== null ? <ErrorPanel message={error} /> : null}
          {loaded && messages.length === 0 ? (
            <StatusPanel>
              {search.trim() !== ""
                ? "No messages match this search."
                : "No readable text messages are saved yet."}
            </StatusPanel>
          ) : null}
          <ThreadList threads={threads} loadDetail={loadDetail} onMute={mute} />
        </div>
        {unseen > 0 ? (
          <button
            type="button"
            className="chat-new-pill"
            onClick={scrollToNewest}
          >
            {unseen} new ↓
          </button>
        ) : null}
      </div>
    </section>
  );
}

/** The muted senders, each with a button to show their messages again. */
function MutedList({
  muted,
  onUnmute,
}: {
  muted: MutedSender[];
  onUnmute: (nodeId: number) => void;
}) {
  return (
    <details className="chat-muted">
      <summary>Muted ({muted.length})</summary>
      <ul>
        {muted.map((sender) => (
          <li key={sender.node_id}>
            <span>{sender.name}</span>
            <button
              type="button"
              className="secondary-button"
              onClick={() => onUnmute(sender.node_id)}
            >
              Unmute
            </button>
          </li>
        ))}
      </ul>
    </details>
  );
}

function ChannelChip({
  label,
  pressed,
  onClick,
}: {
  label: string;
  pressed: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="chat-channel-chip"
      aria-pressed={pressed}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

/**
 * The channel's messages, oldest first: the newest page, older pages the reader
 * asks for, and newer messages polled with after_id while the tab is visible.
 * The page stays bounded however long it is open: while the reader follows the
 * newest messages, the oldest beyond MESSAGE_BUDGET are dropped, and while they
 * read further up, polling pauses once the budget is reached so their place
 * stays put. Starts again from the newest page whenever the URL (the channel)
 * changes, so returning to a channel never brings back an earlier visit's
 * messages.
 */
function useChatMessages(url: string, filter: ChatFilter, following: boolean) {
  const first = useApi<ChatPage>(url);
  const firstPage = first.data;
  const [grown, setGrown] = useState<ChatWindow | null>(null);
  const [older, setOlder] = useState<{
    first: ChatPage;
    loading: boolean;
    error: string | null;
  } | null>(null);
  // Changes made for an earlier first page (another channel) are stale.
  const loaded =
    grown !== null && grown.first === firstPage
      ? grown
      : firstPage === null
        ? null
        : startWindow(firstPage);
  const olderState = older !== null && older.first === firstPage ? older : null;
  const messages = loaded?.messages ?? NO_MESSAGES;
  const paused = !following && messages.length >= MESSAGE_BUDGET;

  const update = useCallback(
    (change: (current: ChatWindow) => ChatWindow) => {
      if (firstPage === null) return;
      setGrown((current) =>
        change(
          current !== null && current.first === firstPage
            ? current
            : startWindow(firstPage),
        ),
      );
    },
    [firstPage],
  );

  // What the next poll needs, kept current without restarting the timer.
  const latest = useRef({ newestId: 0, paused, following });
  useEffect(() => {
    latest.current = {
      newestId: messages.at(-1)?.packet_row_id ?? 0,
      paused,
      following,
    };
  }, [messages, paused, following]);

  useEffect(() => {
    if (firstPage === null) return;
    let controller: AbortController | null = null;
    async function poll() {
      if (document.hidden || controller !== null) return;
      controller = new AbortController();
      try {
        let more = true;
        while (more && !latest.current.paused) {
          const next = await request<ChatPage>(
            chatUrl(filter, latest.current.newestId),
            controller.signal,
          );
          const last = next.messages.at(-1);
          if (last !== undefined) {
            latest.current.newestId = last.packet_row_id;
            const { following } = latest.current;
            update((current) => withNewer(current, next.messages, following));
          }
          more = next.has_newer;
        }
      } catch {
        // Try again at the next interval; the loaded messages stay.
      } finally {
        controller = null;
      }
    }
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
      controller?.abort();
    };
  }, [firstPage, filter, update]);

  const olderRequest = useRef<AbortController | null>(null);
  useEffect(() => () => olderRequest.current?.abort(), [url]);

  function loadOlder() {
    const beforeId = loaded?.nextBeforeId ?? null;
    if (firstPage === null || beforeId === null || olderState?.loading) return;
    olderRequest.current?.abort();
    const controller = new AbortController();
    olderRequest.current = controller;
    setOlder({ first: firstPage, loading: true, error: null });
    request<ChatPage>(withBeforeId(url, beforeId), controller.signal).then(
      (page) => {
        update((current) => ({
          ...current,
          messages: [...page.messages, ...current.messages],
          nextBeforeId: page.next_before_id,
        }));
        setOlder({ first: firstPage, loading: false, error: null });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setOlder({
          first: firstPage,
          loading: false,
          error:
            error instanceof Error
              ? error.message
              : "SigSleuth could not load this history.",
        });
      },
    );
  }

  return {
    messages,
    loaded: firstPage !== null,
    error: first.error,
    nextBeforeId: loaded?.nextBeforeId ?? null,
    loadOlder,
    loadingOlder: olderState?.loading ?? false,
    loadOlderError: olderState?.error ?? null,
  };
}

// One empty list, so an unloaded page does not look new on every render.
const NO_MESSAGES: ChatMessage[] = [];

/**
 * Keeps the reader's place as messages change: open a channel at the newest
 * messages, keep the view still when older ones are added above, and follow
 * new ones only when the reader was already at the newest.
 */
function useScrollAnchoring(
  url: string,
  messages: ChatMessage[],
  atBottom: boolean,
): RefObject<HTMLDivElement | null> {
  const scroller = useRef<HTMLDivElement>(null);
  const previous = useRef<{
    url: string;
    oldestId: number | undefined;
    height: number;
  } | null>(null);
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element === null || messages.length === 0) return;
    const oldestId = messages[0].packet_row_id;
    const before = previous.current;
    if (before === null || before.url !== url || atBottom) {
      element.scrollTop = element.scrollHeight;
    } else if (before.oldestId !== undefined && oldestId < before.oldestId) {
      element.scrollTop += element.scrollHeight - before.height;
    }
    previous.current = { url, oldestId, height: element.scrollHeight };
  }, [url, messages, atBottom]);
  return scroller;
}

type MuteAction = (sender: MutedSender) => void;

function ThreadList({
  threads,
  loadDetail,
  onMute,
}: {
  threads: ChatThread[];
  loadDetail: DetailLoader;
  onMute: MuteAction;
}) {
  let day: string | null = null;
  return threads.map((thread) => {
    const threadDay = new Date(thread.message.received_at).toDateString();
    const divider =
      threadDay !== day ? (
        <div className="chat-day" role="separator">
          <span>
            {new Date(thread.message.received_at).toLocaleDateString(
              undefined,
              {
                weekday: "long",
                year: "numeric",
                month: "long",
                day: "numeric",
              },
            )}
          </span>
        </div>
      ) : null;
    day = threadDay;
    return (
      <div key={thread.message.packet_row_id}>
        {divider}
        <ThreadView thread={thread} loadDetail={loadDetail} onMute={onMute} />
      </div>
    );
  });
}

function ThreadView({
  thread,
  loadDetail,
  onMute,
}: {
  thread: ChatThread;
  loadDetail: DetailLoader;
  onMute: MuteAction;
}) {
  const [expanded, setExpanded] = useState(false);
  const [showEarlier, setShowEarlier] = useState(false);
  const hidden = thread.replies.length - VISIBLE_REPLIES;
  const replies =
    expanded || hidden <= 0
      ? thread.replies
      : thread.replies.slice(0, VISIBLE_REPLIES);
  return (
    <div className="chat-thread">
      {showEarlier
        ? thread.earlier.map((earlier) => (
            <LineView
              key={earlier.message.packet_row_id}
              line={earlier}
              loadDetail={loadDetail}
              onMute={onMute}
            />
          ))
        : null}
      <LineView
        line={thread}
        loadDetail={loadDetail}
        onMute={onMute}
        fold={
          thread.earlier.length > 0 ? (
            <button
              type="button"
              className="chat-fold"
              aria-expanded={showEarlier}
              title={
                showEarlier
                  ? "Hide the earlier repeats"
                  : "Show the earlier repeats"
              }
              onClick={() => setShowEarlier(!showEarlier)}
            >
              Sent {thread.earlier.length + 1} times {showEarlier ? "▴" : "▾"}
            </button>
          ) : null
        }
      />
      {replies.length > 0 ? (
        <div className="chat-replies">
          {replies.map((reply) => (
            <LineView
              key={reply.message.packet_row_id}
              line={reply}
              loadDetail={loadDetail}
              onMute={onMute}
              nested
            />
          ))}
          {hidden > 0 ? (
            <button
              type="button"
              className="chat-more"
              aria-expanded={expanded}
              onClick={() => setExpanded(!expanded)}
            >
              {expanded
                ? "▾ Fewer replies"
                : `▸ ${hidden} more ${hidden === 1 ? "reply" : "replies"}`}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function LineView({
  line,
  loadDetail,
  onMute,
  nested = false,
  fold = null,
}: {
  line: ChatLine;
  loadDetail: DetailLoader;
  onMute: MuteAction;
  nested?: boolean;
  /** The "×N" control of a line that stands for several repeats. */
  fold?: ReactNode;
}) {
  const { message } = line;
  const time = new Date(message.received_at);
  return (
    <div className={`chat-line${nested ? " chat-line-reply" : ""}`}>
      <time
        className="chat-time"
        dateTime={message.received_at}
        title={time.toLocaleString()}
      >
        {time.toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        })}
      </time>
      <div className="chat-body">
        <SenderLink message={message} />
        <MuteButton message={message} onMute={onMute} />
        {message.recipient_node_id !== null ? (
          <span className="chat-recipient">
            →{" "}
            <a
              href={nodeHref(message.recipient_node_id)}
              title={nameTitle(
                message.recipient_node_id,
                message.recipient_short_name,
              )}
            >
              {longLabel(
                message.recipient_node_id,
                message.recipient_long_name,
                message.recipient_short_name,
              )}
            </a>
          </span>
        ) : null}{" "}
        {message.is_reaction ? (
          <span className="chat-text">
            reacted {message.text} to{" "}
            {nested ? "this thread" : <ParentQuote message={message} bare />}
          </span>
        ) : (
          <>
            {!nested && message.reply_id !== null ? (
              <ParentQuote message={message} />
            ) : null}
            <span className="chat-text">{message.text.trimEnd()}</span>
          </>
        )}
        {line.reactions.map((chip) => (
          <span
            key={chip.emoji}
            className="chat-reaction"
            title={chip.reactors.join(", ")}
          >
            {chip.emoji}
            {chip.reactors.length > 1 ? ` ${chip.reactors.length}` : null}
          </span>
        ))}
        {fold}
      </div>
      <div className="chat-meta">
        <RxBadge message={message} loadDetail={loadDetail} />
        <span className="chat-channel">{message.channel}</span>
        <a
          className="chat-open"
          href={`#packets/${message.packet_row_id}`}
          aria-label="Packet details"
          title="Packet details"
        >
          <Icon name="open" />
        </a>
      </div>
    </div>
  );
}

// The hover text of a long-name label: the short name and node ID.
function nameTitle(nodeId: number, shortName: string | null): string {
  const id = formatNodeId(nodeId);
  return shortName?.trim() ? `${shortName.trim()} ${id}` : id;
}

function SenderLink({ message }: { message: ChatMessage }) {
  return (
    <a
      className="chat-sender"
      href={nodeHref(message.sender_node_id)}
      style={{
        color: SENDER_COLOURS[message.sender_node_id % SENDER_COLOURS.length],
      }}
      title={nameTitle(message.sender_node_id, message.sender_short_name)}
    >
      {longLabel(
        message.sender_node_id,
        message.sender_long_name,
        message.sender_short_name,
      )}
    </a>
  );
}

function MuteButton({
  message,
  onMute,
}: {
  message: ChatMessage;
  onMute: MuteAction;
}) {
  const name = longLabel(
    message.sender_node_id,
    message.sender_long_name,
    message.sender_short_name,
  );
  return (
    <button
      type="button"
      className="chat-mute"
      aria-label={`Mute ${name}`}
      title={`Mute ${name}: hide their messages`}
      onClick={() => onMute({ node_id: message.sender_node_id, name })}
    >
      <Icon name="mute" />
    </button>
  );
}

// The message a reply or reaction answers, when it is not loaded on the page
// (a loaded one has the reply nested under it instead). The quote is cut to
// one line; clicking it shows the whole message and a link to its packet.
// A parent saved but unreadable links straight to its packet; one never
// captured has nothing to open.
function ParentQuote({
  message,
  bare = false,
}: {
  message: ChatMessage;
  bare?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const parent = message.reply_to;
  const arrow = bare ? "" : "↩ ";
  if (parent === null) {
    return (
      <span
        className="chat-quote chat-quote-missing"
        title="Original not received"
      >
        {arrow}a message not captured
      </span>
    );
  }
  const sender = longLabel(
    parent.sender_node_id,
    parent.sender_long_name,
    parent.sender_short_name,
  );
  if (parent.text === null) {
    return (
      <a
        className="chat-quote"
        href={`#packets/${parent.packet_row_id}`}
        title="Saved but encrypted with a key SigSleuth does not have"
      >
        {arrow}
        {sender}: unreadable message
      </a>
    );
  }
  return (
    <>
      <button
        type="button"
        className={`chat-quote${open ? " chat-quote-open" : ""}`}
        aria-expanded={open}
        title={open ? "Shorten the quote" : "Show the whole message"}
        onClick={() => setOpen(!open)}
      >
        {arrow}
        {sender}: {parent.text}
      </button>
      {open ? (
        <a
          className="chat-quote-packet"
          href={`#packets/${parent.packet_row_id}`}
        >
          Packet details
          <Icon name="open" />
        </a>
      ) : null}
    </>
  );
}

// "Reported by 8 gateways, up to 84 km"; hovering lists each gateway's
// report. Red when only one gateway reported it: it did not get far.
function RxBadge({
  message,
  loadDetail,
}: {
  message: ChatMessage;
  loadDetail: DetailLoader;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span
      className="chat-rx"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <button
        type="button"
        className={`chat-rx-badge${message.reception_count === 1 ? " chat-rx-lone" : ""}`}
        aria-expanded={open}
      >
        <span className="chat-rx-lead">Reported by </span>
        {message.reception_count}{" "}
        {message.reception_count === 1 ? "gateway" : "gateways"}
        {message.farthest_km !== null
          ? `, up to ${formatKm(message.farthest_km)} km`
          : null}
      </button>
      {open ? (
        <ReceptionPopover
          packetRowId={message.packet_row_id}
          loadDetail={loadDetail}
        />
      ) : null}
    </span>
  );
}

function ReceptionPopover({
  packetRowId,
  loadDetail,
}: {
  packetRowId: number;
  loadDetail: DetailLoader;
}) {
  const [state, setState] = useState<{
    detail: PacketDetail | null;
    error: string | null;
  }>({ detail: null, error: null });
  useEffect(() => {
    let current = true;
    loadDetail(packetRowId).then(
      (detail) => current && setState({ detail, error: null }),
      (error: unknown) =>
        current &&
        setState({
          detail: null,
          error:
            error instanceof Error
              ? error.message
              : "Could not load gateway reports.",
        }),
    );
    return () => {
      current = false;
    };
  }, [packetRowId, loadDetail]);

  // Open above the badge when the chat scroller would clip it below.
  const popover = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = popover.current;
    const badge = element?.parentElement;
    const scroller = element?.closest(".chat-scroller");
    if (!element || !badge || !scroller) return;
    const bounds = scroller.getBoundingClientRect();
    const anchor = badge.getBoundingClientRect();
    const below = bounds.bottom - anchor.bottom;
    const above = anchor.top - bounds.top;
    element.classList.toggle(
      "chat-popover-above",
      element.offsetHeight > below && above > below,
    );
  }, [state]);

  const { detail, error } = state;
  return (
    <div
      ref={popover}
      className="chat-popover"
      role="dialog"
      aria-label="Gateway reports"
    >
      {error !== null ? <p className="chat-popover-error">{error}</p> : null}
      {detail === null && error === null ? <p>Loading…</p> : null}
      {detail !== null ? (
        <>
          <p>
            <a href={`#packets/${packetRowId}`}>
              ↗ Packet, {detail.packet.channel}
            </a>
          </p>
          <table>
            <thead>
              <tr>
                <th scope="col">Gateway</th>
                <th scope="col" className="number">
                  Hops
                </th>
                <th scope="col" className="number">
                  RSSI
                </th>
                <th scope="col" className="number">
                  SNR
                </th>
                <th scope="col">Relay</th>
              </tr>
            </thead>
            <tbody>
              {detail.receptions.map((reception) => (
                <tr key={reception.reception_id}>
                  <td>
                    <a
                      href={nodeHref(reception.gateway_node_id)}
                      title={nameTitle(
                        reception.gateway_node_id,
                        reception.gateway_short_name,
                      )}
                    >
                      {longLabel(
                        reception.gateway_node_id,
                        reception.gateway_name,
                        reception.gateway_short_name,
                      )}
                    </a>
                  </td>
                  <td className="number">{hopsUsed(reception)}</td>
                  <td className="number">{reception.rx_rssi ?? "—"}</td>
                  <td className="number">{reception.rx_snr ?? "—"}</td>
                  <RelayCell reception={reception} />
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </div>
  );
}

/** Loads a packet's detail once per page; a failed load is tried again next time. */
function useDetailCache(): DetailLoader {
  const [cache] = useState(() => new Map<number, Promise<PacketDetail>>());
  const [load] = useState(() => (packetRowId: number) => {
    let detail = cache.get(packetRowId);
    if (detail === undefined) {
      detail = request<PacketDetail>(
        packetUrl(packetRowId),
        new AbortController().signal,
      );
      detail.catch(() => cache.delete(packetRowId));
      cache.set(packetRowId, detail);
    }
    return detail;
  });
  return load;
}
