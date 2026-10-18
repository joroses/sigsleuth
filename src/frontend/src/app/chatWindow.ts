import { ChatMessage, ChatPage } from "./api";

// The most messages kept while following new ones; older ones are dropped
// from the top, and "Load older" fetches them again. ChatView pauses polling
// at this budget while the reader is scrolled up, preserving their place;
// deliberately loading older pages may exceed it.
export const MESSAGE_BUDGET = 1_000;

/** The loaded messages, grown from the newest page they started with. */
export interface ChatWindow {
  first: ChatPage;
  /** Oldest first. */
  messages: ChatMessage[];
  /** Where "Load older" continues; null when nothing older is saved. */
  nextBeforeId: number | null;
}

export function startWindow(first: ChatPage): ChatWindow {
  return {
    first,
    messages: first.messages,
    nextBeforeId: first.next_before_id,
  };
}

/** Add newer messages; while following them, keep only the newest MESSAGE_BUDGET. */
export function withNewer(
  loaded: ChatWindow,
  newer: ChatMessage[],
  following: boolean,
): ChatWindow {
  const messages = [...loaded.messages, ...newer];
  if (!following || messages.length <= MESSAGE_BUDGET) {
    return { ...loaded, messages };
  }
  const kept = messages.slice(-MESSAGE_BUDGET);
  return { ...loaded, messages: kept, nextBeforeId: kept[0].packet_row_id };
}
