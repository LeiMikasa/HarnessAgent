export type Mode = "read-only" | "workspace" | "full";
export type Metadata = {
  id: string;
  title: string;
  workdir: string;
  permission_mode: Mode;
  provider: string;
  model: string;
  created_at: string;
  updated_at: string;
  busy?: boolean;
};
export type Entry = {
  id: string;
  kind: "message" | "tool" | "notice";
  text: string;
  thinking?: string;
  thinking_status?: "streaming" | "done" | "interrupted";
  role?: "user" | "assistant";
  owner?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  status?: string;
  elapsed?: number;
  result_chars?: number;
};
export type Approval = {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
  owner: string;
  workdir: string;
  host_execution: boolean;
};
export type Usage = {
  estimated_input_tokens: number;
  input_token_limit: number | null;
  estimated_input_token_breakdown?: Record<string, number>;
};
export type Session = {
  metadata: Metadata;
  timeline: Entry[];
  busy: boolean;
  cancel_requested: boolean;
  activity: string;
  pending_approvals: Approval[];
  pending_settings?: Partial<
    Pick<Metadata, "permission_mode" | "provider" | "model">
  >;
  context_usage: Usage | null;
  metrics: {
    counts?: Record<string, number>;
    token_totals?: Record<string, number | null>;
  };
  todos: { content: string; status: string }[];
  last_error: string;
  seq: number;
};
export type Config = {
  workdir: string;
  provider: string;
  model: string;
  has_api_key: boolean;
  permission_mode: Mode;
  permission_modes: { value: Mode; label: string }[];
};
export const modeLabels: Record<Mode, string> = {
  "read-only": "只读",
  workspace: "工作区内修改",
  full: "完全权限",
};
