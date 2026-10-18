import { describe, expect, it } from "vitest";

import { ChatWindow, MESSAGE_BUDGET, withNewer } from "./chatWindow";
import { ChatMessage } from "./api";

function messages(from: number, to: number): ChatMessage[] {
  return Array.from(
    { length: to - from + 1 },
    (_, index) => ({ packet_row_id: from + index }) as ChatMessage,
  );
}

function loaded(from: number, to: number): ChatWindow {
  const first = { messages: [], next_before_id: null, has_newer: false };
  return { first, messages: messages(from, to), nextBeforeId: null };
}

const ids = (window: ChatWindow) =>
  window.messages.map((message) => message.packet_row_id);

describe("withNewer", () => {
  it("keeps only the newest messages while following them", () => {
    const next = withNewer(
      loaded(1, MESSAGE_BUDGET),
      messages(MESSAGE_BUDGET + 1, MESSAGE_BUDGET + 3),
      true,
    );

    expect(next.messages).toHaveLength(MESSAGE_BUDGET);
    expect(ids(next)[0]).toBe(4);
    expect(ids(next).at(-1)).toBe(MESSAGE_BUDGET + 3);
    // "Load older" continues just before the oldest kept message.
    expect(next.nextBeforeId).toBe(4);
  });

  it("keeps everything while the reader is further up", () => {
    const next = withNewer(
      loaded(1, MESSAGE_BUDGET),
      messages(MESSAGE_BUDGET + 1, MESSAGE_BUDGET + 3),
      false,
    );

    expect(next.messages).toHaveLength(MESSAGE_BUDGET + 3);
    expect(next.nextBeforeId).toBeNull();
  });

  it("keeps everything under the budget", () => {
    const next = withNewer(loaded(1, 10), messages(11, 12), true);

    expect(ids(next)).toEqual(messages(1, 12).map((m) => m.packet_row_id));
    expect(next.nextBeforeId).toBeNull();
  });
});
