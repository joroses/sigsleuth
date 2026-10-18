import { ChatMessage } from "./api";
import { longLabel } from "./nodeDisplay";

/** Reactions with the same emoji on one message; reactors are long names. */
export interface ReactionChip {
  emoji: string;
  reactors: string[];
}

export interface ChatLine {
  message: ChatMessage;
  reactions: ReactionChip[];
}

/**
 * A top-level line with the replies nested under it, oldest first. earlier
 * holds the repeats folded into it, oldest first (see foldRepeats).
 */
export interface ChatThread extends ChatLine {
  replies: ChatLine[];
  earlier: ChatThread[];
}

// A repeat folds into the previous one when it comes within this time.
const REPEAT_GAP_MS = 60 * 60 * 1000;

/**
 * Arrange loaded messages (oldest first) into threads, within the loaded
 * messages only: a reaction to a loaded message becomes a chip on it, and a
 * reply to a loaded message goes under that message's thread root, so replies
 * to replies flatten to one level. Anything whose parent is not loaded stays a
 * top-level line, which the page shows quoting the parent.
 */
export function chatThreads(messages: ChatMessage[]): ChatThread[] {
  const byId = new Map(
    messages.map((message) => [message.packet_row_id, message]),
  );
  const lines = new Map<number, ChatLine>();
  const line = (message: ChatMessage): ChatLine => {
    let found = lines.get(message.packet_row_id);
    if (found === undefined) {
      found = { message, reactions: [] };
      lines.set(message.packet_row_id, found);
    }
    return found;
  };
  const parentOf = (message: ChatMessage): ChatMessage | undefined => {
    const id = message.reply_to?.packet_row_id;
    return id === undefined || id === message.packet_row_id
      ? undefined
      : byId.get(id);
  };
  const rootOf = (message: ChatMessage): ChatMessage => {
    const seen = new Set<number>();
    let current = message;
    for (
      let parent = parentOf(current);
      parent !== undefined &&
      !parent.is_reaction &&
      !seen.has(parent.packet_row_id);
      parent = parentOf(current)
    ) {
      seen.add(current.packet_row_id);
      current = parent;
    }
    return current;
  };

  const threads: ChatThread[] = [];
  const threadByRoot = new Map<number, ChatThread>();
  for (const message of messages) {
    const parent = parentOf(message);
    if (parent !== undefined && message.is_reaction) {
      addReaction(line(parent).reactions, message);
      continue;
    }
    const root = parent === undefined ? message : rootOf(message);
    if (root !== message) {
      const thread = threadByRoot.get(root.packet_row_id);
      if (thread !== undefined) {
        thread.replies.push(line(message));
        continue;
      }
    }
    const thread: ChatThread = Object.assign(line(message), {
      replies: [],
      earlier: [],
    });
    threads.push(thread);
    threadByRoot.set(message.packet_row_id, thread);
  }
  return foldRepeats(threads);
}

/**
 * Fold repeated messages, such as a sender posting readings as text, into the
 * newest one: same sender, recipient and channel, the same text once each run
 * of digits is ignored, and each within an hour of the previous. Messages that
 * differ only in numbers fold too; that approximation is accepted. Replies,
 * reactions and messages with replies never fold.
 */
function foldRepeats(threads: ChatThread[]): ChatThread[] {
  const latest = new Map<string, ChatThread>();
  const folded = new Set<ChatThread>();
  for (const thread of threads) {
    const { message } = thread;
    if (
      message.reply_id !== null ||
      message.is_reaction ||
      thread.replies.length > 0
    ) {
      continue;
    }
    const key = [
      message.sender_node_id,
      message.recipient_node_id,
      message.channel,
      message.text.replace(/\d+/g, "#"),
    ].join(" ");
    const previous = latest.get(key);
    if (
      previous !== undefined &&
      Date.parse(message.received_at) -
        Date.parse(previous.message.received_at) <
        REPEAT_GAP_MS
    ) {
      // Hand the chain over instead of copying it, so a long run of
      // repeats costs one push each.
      const earlier = previous.earlier;
      previous.earlier = [];
      earlier.push(previous);
      thread.earlier = earlier;
      folded.add(previous);
    }
    latest.set(key, thread);
  }
  return threads.filter((thread) => !folded.has(thread));
}

function addReaction(chips: ReactionChip[], reaction: ChatMessage) {
  const emoji = reaction.text;
  const reactor = longLabel(
    reaction.sender_node_id,
    reaction.sender_long_name,
    reaction.sender_short_name,
  );
  const chip = chips.find((chip) => chip.emoji === emoji);
  if (chip === undefined) chips.push({ emoji, reactors: [reactor] });
  else chip.reactors.push(reactor);
}
