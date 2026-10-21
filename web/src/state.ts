import type { Entry, Session } from "./types";

export function upsertEntry(entries: Entry[], next: Entry): Entry[] {
  const index = entries.findIndex((entry) => entry.id === next.id);
  if (index < 0) return [...entries, next].slice(-500);
  const result = entries.slice();
  result[index] = { ...result[index], ...next };
  return result;
}

export function mergeState(current: Session, patch: Partial<Session>): Session {
  return { ...current, ...patch, timeline: patch.timeline ?? current.timeline };
}
