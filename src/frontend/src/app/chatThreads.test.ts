import { describe, expect, it } from "vitest";

import type { ChatMessage } from "./api";
import { chatThreads } from "./chatThreads";

function message(
  id: number,
  fields: Partial<ChatMessage> = {},
  parent?: number,
): ChatMessage {
  return {
    packet_row_id: id,
    packet_id: id,
    received_at: "2026-09-19T09:00:00Z",
    sender_node_id: 0x11111111,
    sender_short_name: "HILL",
    sender_long_name: null,
    recipient_node_id: null,
    recipient_short_name: null,
    recipient_long_name: null,
    channel: "LongFast",
    text: `message ${id}`,
    is_reaction: false,
    reply_id: parent ?? null,
    reply_to:
      parent === undefined
        ? null
        : {
            packet_row_id: parent,
            sender_node_id: 0x11111111,
            sender_short_name: "HILL",
            sender_long_name: null,
            text: `message ${parent}`,
          },
    reception_count: 1,
    farthest_km: null,
    ...fields,
  };
}

function shape(messages: ChatMessage[]) {
  return chatThreads(messages).map((thread) => [
    thread.message.packet_row_id,
    thread.replies.map((reply) => reply.message.packet_row_id),
  ]);
}

describe("chatThreads", () => {
  it("puts reactions to a loaded message on it as chips", () => {
    const threads = chatThreads([
      message(1),
      message(2, { is_reaction: true, text: "👍", sender_short_name: "A" }, 1),
      message(3, { is_reaction: true, text: "👍", sender_short_name: "B" }, 1),
    ]);

    expect(threads).toHaveLength(1);
    expect(threads[0].reactions).toEqual([
      { emoji: "👍", reactors: ["A", "B"] },
    ]);
  });

  it("nests a reply under a loaded parent and flattens replies to replies", () => {
    expect(
      shape([message(1), message(2), message(3, {}, 1), message(4, {}, 3)]),
    ).toEqual([
      [1, [3, 4]],
      [2, []],
    ]);
  });

  it("keeps a reply or reaction whose parent is not loaded at the top level", () => {
    const threads = chatThreads([
      message(2, {}, 1),
      message(3, { is_reaction: true, text: "👍" }, 1),
    ]);

    expect(threads.map((thread) => thread.message.packet_row_id)).toEqual([
      2, 3,
    ]);
  });

  it("moves replies under a parent once older messages bring it in", () => {
    expect(shape([message(2, {}, 1)])).toEqual([[2, []]]);
    expect(shape([message(1), message(2, {}, 1)])).toEqual([[1, [2]]]);
  });

  describe("folding repeats", () => {
    const at = (minutes: number) =>
      new Date(Date.UTC(2026, 8, 19, 9, minutes)).toISOString();
    const folds = (messages: ChatMessage[]) =>
      chatThreads(messages).map((thread) => [
        thread.message.packet_row_id,
        thread.earlier.map((earlier) => earlier.message.packet_row_id),
      ]);

    it("folds identical texts and texts differing only in numbers", () => {
      expect(
        folds([
          message(1, { text: "E=1;V=243.60", received_at: at(0) }),
          message(2, { text: "other", sender_node_id: 2, received_at: at(1) }),
          message(3, { text: "E=1;V=244.10", received_at: at(10) }),
          message(4, { text: "E=1;V=244.10", received_at: at(20) }),
        ]),
      ).toEqual([
        [2, []],
        [4, [1, 3]],
      ]);
    });

    it("keeps different senders, different words and long gaps apart", () => {
      expect(
        folds([
          message(1, { text: "hello", received_at: at(0) }),
          message(2, { text: "hello", sender_node_id: 2, received_at: at(1) }),
          message(3, { text: "goodbye", received_at: at(2) }),
          message(4, { text: "hello", received_at: at(61) }),
        ]).map(([id, earlier]) => [id, earlier]),
      ).toEqual([
        [1, []],
        [2, []],
        [3, []],
        [4, []],
      ]);
    });

    it("keeps different recipients and channels apart", () => {
      expect(
        folds([
          message(1, { text: "hi", recipient_node_id: 2 }),
          message(2, { text: "hi", recipient_node_id: 3 }),
          message(3, { text: "hi", channel: "MediumFast" }),
          message(4, { text: "hi" }),
        ]),
      ).toEqual([
        [1, []],
        [2, []],
        [3, []],
        [4, []],
      ]);
    });

    it("never folds a reply", () => {
      expect(
        folds([message(1, { text: "ok" }), message(2, { text: "ok" }, 99)]),
      ).toEqual([
        [1, []],
        [2, []],
      ]);
    });
  });
});
