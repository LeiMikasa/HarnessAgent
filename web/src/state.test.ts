import { describe, it, expect } from "vitest";
import { upsertEntry, mergeState } from "./state";
import type { Entry, Session } from "./types";

describe("SSE view projection", () => {
  it("replaces a streaming message on final output and ignores duplicate delivery", () => {
    const entry: Entry = {
      id: "model-a",
      kind: "message",
      role: "assistant",
      text: "你",
      status: "running",
    };
    let timeline = upsertEntry([], entry);
    timeline = upsertEntry(timeline, {
      ...entry,
      text: "你好",
      status: "done",
    });
    timeline = upsertEntry(timeline, {
      ...entry,
      text: "你好",
      status: "done",
    });
    expect(timeline).toHaveLength(1);
    expect(timeline[0]!.text).toBe("你好");
    expect(timeline[0]!.status).toBe("done");
  });
  it("retains tool arguments and ordering when execution finishes", () => {
    const tool: Entry = {
      id: "tool-a",
      kind: "tool",
      name: "read_file",
      arguments: { path: "a.txt" },
      text: "",
      status: "running",
    };
    const result = upsertEntry([tool], {
      ...tool,
      text: "content",
      status: "done",
    });
    expect(result[0]!.arguments).toEqual({ path: "a.txt" });
    expect(result[0]!.text).toBe("content");
  });
  it("state updates preserve conversation entries", () => {
    const session = {
      timeline: [{ id: "user-a", text: "hello" }],
      busy: true,
    } as Session;
    expect(mergeState(session, { busy: false }).timeline).toEqual(
      session.timeline,
    );
    expect(mergeState(session, { busy: false }).busy).toBe(false);
  });
});
