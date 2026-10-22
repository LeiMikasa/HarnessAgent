<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import {
  PhPlus,
  PhChatCircle,
  PhFolder,
  PhArrowUp,
  PhStop,
  PhShieldCheck,
  PhCaretDown,
  PhSidebarSimple,
  PhArrowRight,
  PhCheck,
  PhCircle,
  PhCircleNotch,
  PhX,
  PhCode,
  PhMagnifyingGlass,
  PhWrench,
  PhFlask,
  PhWifiSlash,
} from "@phosphor-icons/vue";
import MessageEntry from "./components/MessageEntry.vue";
import ApprovalDialog from "./components/ApprovalDialog.vue";
import SessionSettingsDialog from "./components/SessionSettingsDialog.vue";
import { api } from "./api";
import { mergeState, upsertEntry } from "./state";
import { modeLabels } from "./types";
import type { Config, Entry, Metadata, Mode, Session } from "./types";

const config = ref<Config>();
const sessions = ref<Metadata[]>([]);
const active = ref<Session>();
const draft = ref("");
const error = ref("");
const ready = ref(false);
const sending = ref(false);
const connected = ref(false);
const sidebar = ref(false);
const showNew = ref(false);
const creating = ref(false);
const pickingDirectory = ref(false);
const approvalWaiting = ref(false);
const settingsKind = ref<"permission" | "model">();
const chat = ref<HTMLElement>();
const composer = ref<HTMLTextAreaElement>();
const newDialog = ref<HTMLDialogElement>();
const workspaceInput = ref<HTMLInputElement>();
const follow = ref(true);
const form = ref({
  workdir: "",
  permission_mode: "workspace" as Mode,
  mock: false,
});
let source: EventSource | undefined;
let selection = 0;
let previousFocus: HTMLElement | null = null;
const currentApproval = computed(() => active.value?.pending_approvals[0]);
const usage = computed(() => active.value?.context_usage);
const occupied = computed(() => {
  const value = usage.value;
  return value?.input_token_limit
    ? Math.min(
        100,
        (value.estimated_input_tokens / value.input_token_limit) * 100,
      )
    : 0;
});
const completed = computed(
  () =>
    active.value?.todos.filter((todo) => todo.status === "completed").length ??
    0,
);
const breakdown = [
  ["system_prompt", "提示词"],
  ["tool_definitions", "工具定义"],
  ["tool_results", "工具结果"],
  ["conversation", "对话消息"],
  ["request_structure", "请求结构"],
];
const suggestions = [
  {
    icon: PhMagnifyingGlass,
    title: "熟悉这个项目",
    text: "先阅读项目结构，告诉我如何启动，以及核心代码在哪里。",
  },
  {
    icon: PhWrench,
    title: "一起排查问题",
    text: "帮我检查当前项目可能存在的问题，先给出具体依据再修改。",
  },
  { icon: PhCode, title: "实现一个功能", text: "我想在当前项目新增一个功能：" },
  {
    icon: PhFlask,
    title: "检查与测试",
    text: "检查项目现有测试和验证方式，找出需要补充的关键检查。",
  },
];

function number(value: number | null | undefined) {
  return value == null ? "未知" : value.toLocaleString();
}
function setError(value: unknown) {
  error.value = value instanceof Error ? value.message : String(value);
}
function upsertSession(metadata: Metadata) {
  sessions.value = [
    metadata,
    ...sessions.value.filter((session) => session.id !== metadata.id),
  ].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}
function connect(id: string, generation: number) {
  source?.close();
  connected.value = false;
  source = new EventSource(`/api/sessions/${id}/events`);
  const valid = () => generation === selection;
  source.addEventListener("snapshot", (event) => {
    if (!valid()) return;
    active.value = JSON.parse((event as MessageEvent).data);
    connected.value = true;
    upsertSession({ ...active.value!.metadata, busy: active.value!.busy });
  });
  source.addEventListener("state", (event) => {
    if (!valid() || !active.value) return;
    active.value = mergeState(
      active.value,
      JSON.parse((event as MessageEvent).data),
    );
    upsertSession({ ...active.value.metadata, busy: active.value.busy });
  });
  source.addEventListener("entry", (event) => {
    if (!valid() || !active.value) return;
    active.value.timeline = upsertEntry(
      active.value.timeline,
      JSON.parse((event as MessageEvent).data) as Entry,
    );
  });
  source.onopen = () => {
    if (valid()) connected.value = true;
  };
  source.onerror = () => {
    if (valid()) connected.value = false;
  };
}
async function selectSession(id: string) {
  settingsKind.value = undefined;
  const generation = ++selection;
  source?.close();
  sidebar.value = false;
  try {
    const snapshot = await api<Session>(`/sessions/${id}`);
    if (generation !== selection) return;
    active.value = snapshot;
    follow.value = true;
    history.replaceState(null, "", `?session=${id}`);
    connect(id, generation);
    await nextTick();
    chat.value?.scrollTo({ top: chat.value.scrollHeight });
  } catch (value) {
    setError(value);
  }
}
function openNew() {
  error.value = "";
  form.value = {
    workdir: config.value?.workdir ?? "",
    permission_mode: "workspace",
    mock: !config.value?.has_api_key || config.value.provider === "mock",
  };
  showNew.value = true;
}
watch(showNew, async (value) => {
  if (value) {
    previousFocus = document.activeElement as HTMLElement;
    await nextTick();
    newDialog.value?.showModal();
    workspaceInput.value?.focus();
  } else {
    newDialog.value?.close();
    previousFocus?.focus();
  }
});
async function createSession() {
  if (creating.value || pickingDirectory.value) return;
  creating.value = true;
  try {
    const result = await api<Session>("/sessions", form.value);
    upsertSession(result.metadata);
    showNew.value = false;
    await selectSession(result.metadata.id);
    composer.value?.focus();
  } catch (value) {
    setError(value);
  } finally {
    creating.value = false;
  }
}
async function browseDirectory() {
  if (pickingDirectory.value || creating.value) return;
  pickingDirectory.value = true;
  error.value = "";
  const targetForm = form.value;
  try {
    const result = await api<{ path: string | null }>("/workspaces/pick", {
      initial_path: targetForm.workdir,
    });
    if (result.path && showNew.value && form.value === targetForm) {
      form.value.workdir = result.path;
    }
  } catch (value) {
    setError(value);
  } finally {
    pickingDirectory.value = false;
    await nextTick();
    if (showNew.value) workspaceInput.value?.focus();
  }
}
async function send() {
  const text = draft.value.trim();
  if (!text || !active.value || active.value.busy || sending.value) return;
  sending.value = true;
  follow.value = true;
  try {
    await api(`/sessions/${active.value.metadata.id}/messages`, { text });
    draft.value = "";
    if (composer.value) composer.value.style.height = "auto";
  } catch (value) {
    setError(value);
  } finally {
    sending.value = false;
  }
}
async function stop() {
  if (!active.value) return;
  try {
    await api(`/sessions/${active.value.metadata.id}/stop`, {});
  } catch (value) {
    setError(value);
  }
}
async function decide(allowed: boolean) {
  if (!active.value || !currentApproval.value || approvalWaiting.value) return;
  const requestId = currentApproval.value.id;
  approvalWaiting.value = true;
  try {
    await api(`/sessions/${active.value.metadata.id}/approvals/${requestId}`, {
      allowed,
    });
    active.value.pending_approvals = active.value.pending_approvals.filter(
      (item) => item.id !== requestId,
    );
  } catch (value) {
    setError(value);
  } finally {
    approvalWaiting.value = false;
  }
}
function resizeComposer() {
  if (!composer.value) return;
  composer.value.style.height = "auto";
  composer.value.style.height =
    Math.min(composer.value.scrollHeight, 180) + "px";
}
function keyboard(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void send();
  }
}
function choosePrompt(text: string) {
  draft.value = text;
  nextTick(() => {
    resizeComposer();
    composer.value?.focus();
  });
}
function scrollChanged() {
  if (chat.value)
    follow.value =
      chat.value.scrollHeight - chat.value.scrollTop - chat.value.clientHeight <
      100;
}
watch(
  () =>
    active.value?.timeline
      .map(
        (entry) =>
          `${entry.id}:${entry.text.length}:${entry.thinking?.length ?? 0}:${entry.status}`,
      )
      .join(","),
  async () => {
    await nextTick();
    if (follow.value) chat.value?.scrollTo({ top: chat.value.scrollHeight });
  },
);
onMounted(async () => {
  try {
    const [defaults, list] = await Promise.all([
      api<Config>("/config"),
      api<Metadata[]>("/sessions"),
    ]);
    config.value = defaults;
    sessions.value = list;
    const requested = new URLSearchParams(location.search).get("session");
    if (requested && list.some((item) => item.id === requested))
      await selectSession(requested);
    else if (list.length) await selectSession(list[0]!.id);
  } catch (value) {
    setError(value);
  } finally {
    ready.value = true;
  }
});
onBeforeUnmount(() => source?.close());
</script>

<template>
  <div class="workspace-shell">
    <button
      v-if="sidebar"
      class="sidebar-scrim"
      aria-label="关闭会话列表"
      @click="sidebar = false"
    ></button>
    <aside class="sidebar" :class="{ expanded: sidebar }" aria-label="会话导航">
      <a class="brand" href="/" aria-label="minicode 工作台"
        ><span class="brand-mark">m<span></span></span
        ><span>mini<span class="brand-light">code</span></span></a
      >
      <button class="new-session" :disabled="!ready" @click="openNew">
        <PhPlus :size="18" />新建会话<span class="shortcut">NEW</span>
      </button>
      <div class="nav-heading">
        工作会话<span>{{ sessions.length }}</span>
      </div>
      <nav class="session-list" aria-label="历史会话">
        <button
          v-for="session in sessions"
          :key="session.id"
          :class="{ selected: active?.metadata.id === session.id }"
          @click="selectSession(session.id)"
        >
          <PhCircleNotch
            v-if="session.busy"
            :size="17"
            class="spin"
          /><PhChatCircle v-else :size="17" /> <span>{{ session.title }}</span
          ><span v-if="session.provider === 'mock'" class="session-mock"
            >Mock</span
          >
        </button>
        <p v-if="ready && !sessions.length" class="no-sessions">
          会话会保存在本机。<br />从一个新任务开始。
        </p>
      </nav>
      <div class="sidebar-bottom">
        <span class="local-dot"></span>
        <div>
          <strong>本地工作台</strong><small>文件与会话保留在你的电脑</small>
        </div>
        <span class="version">v0.1</span>
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <div class="topbar-title">
          <button
            class="icon-button mobile-menu"
            aria-label="打开会话列表"
            @click="sidebar = !sidebar"
          >
            <PhSidebarSimple :size="21" /></button
          ><span class="breadcrumb">工作台</span
          ><span class="breadcrumb-separator">/</span
          ><strong>{{ active?.metadata.title ?? "新任务" }}</strong>
        </div>
        <div v-if="active" class="session-controls">
          <button
            class="permission-chip model-chip"
            aria-label="切换模型"
            @click="settingsKind = 'model'"
            :title="active.metadata.model"
          >
            <PhCircleNotch
              v-if="
                active.pending_settings?.model ||
                active.pending_settings?.provider
              "
              class="spin"
              :size="14"
            />
            <span>{{
              active.metadata.provider === "mock"
                ? "Mock · 免费"
                : active.metadata.model
            }}</span
            ><PhCaretDown :size="12" />
          </button>
          <button
            class="permission-chip"
            :class="active.metadata.permission_mode"
            @click="settingsKind = 'permission'"
            aria-label="调整权限"
            title="调整当前会话权限"
          >
            <PhCircleNotch
              v-if="active.pending_settings?.permission_mode"
              class="spin"
              :size="14"
            />
            <PhShieldCheck :size="16" />{{
              modeLabels[active.metadata.permission_mode]
            }}<PhCaretDown :size="12" />
          </button>
        </div>
        <span v-else class="topbar-note"
          ><span class="local-dot"></span> 本地运行</span
        >
      </header>

      <div class="work-area">
        <section class="conversation" aria-label="Agent 对话">
          <div v-if="active" class="workspace-strip">
            <PhFolder :size="15" /><span :title="active.metadata.workdir">{{
              active.metadata.workdir
            }}</span
            ><span
              v-if="Object.keys(active.pending_settings ?? {}).length"
              class="strip-model"
              role="status"
              >设置等待当前调用结束后生效</span
            >
          </div>
          <div v-if="error" class="error-banner" role="alert">
            <span>{{ error }}</span
            ><button
              class="icon-button"
              aria-label="关闭错误提示"
              @click="error = ''"
            >
              <PhX :size="16" />
            </button>
          </div>
          <div
            v-if="active && !connected"
            class="connection-banner"
            role="status"
          >
            <PhWifiSlash :size="16" />正在连接事件流，恢复后会同步最新状态。
          </div>
          <div ref="chat" class="chat-scroll" @scroll="scrollChanged">
            <div v-if="!active || !active.timeline.length" class="empty-state">
              <div class="empty-symbol"><span>m</span><i></i></div>
              <p class="eyebrow">你的本地编码助手</p>
              <h1>把想法，变成下一步。</h1>
              <p class="empty-description">
                读懂项目、解决问题、完成修改。<br />每一步工具操作，都清楚可见。
              </p>
              <div v-if="active" class="suggestions">
                <button
                  v-for="item in suggestions"
                  :key="item.title"
                  @click="choosePrompt(item.text)"
                >
                  <component :is="item.icon" :size="20" /><span>{{
                    item.title
                  }}</span
                  ><PhArrowRight :size="15" />
                </button>
              </div>
              <template v-else
                ><button
                  class="button primary welcome-action"
                  :disabled="!ready"
                  @click="openNew"
                >
                  {{ ready ? "创建第一个会话" : "正在连接工作台…"
                  }}<PhArrowRight :size="17" />
                </button>
                <p class="welcome-note">
                  先选择项目目录与权限，再开始工作
                </p></template
              >
            </div>
            <div v-else class="timeline">
              <MessageEntry
                v-for="entry in active.timeline"
                :key="entry.id"
                :entry="entry"
              />
              <div v-if="active.busy" class="working-status" role="status">
                <PhCircleNotch :size="16" class="spin" />{{ active.activity }}
              </div>
            </div>
          </div>
          <div class="composer-area">
            <div v-if="active?.metadata.provider === 'mock'" class="mock-note">
              Mock 演示只验证运行流程，不调用真实模型。
            </div>
            <form class="composer" @submit.prevent="send">
              <textarea
                ref="composer"
                v-model="draft"
                aria-label="输入任务"
                :placeholder="
                  active
                    ? '描述你想完成的任务…'
                    : '创建会话后，开始描述你的任务…'
                "
                rows="2"
                :disabled="!active || active.busy"
                @input="resizeComposer"
                @keydown="keyboard"
              ></textarea>
              <div class="composer-bottom">
                <span class="composer-model"
                  ><span class="local-dot"></span
                  >{{ active ? active.metadata.model : "等待选择项目" }}</span
                ><button
                  v-if="active?.busy"
                  class="send-button stop-button"
                  type="button"
                  :disabled="active.cancel_requested"
                  aria-label="停止任务"
                  @click="stop"
                >
                  <PhStop :size="17" weight="fill" /></button
                ><button
                  v-else
                  class="send-button"
                  type="submit"
                  aria-label="发送任务"
                  :disabled="!active || !draft.trim() || sending"
                >
                  <PhArrowUp :size="21" weight="bold" />
                </button>
              </div>
            </form>
            <div class="composer-hint">
              <span>{{
                active?.busy
                  ? "停止会在当前调用结束后生效"
                  : "Enter 发送 · Shift + Enter 换行"
              }}</span
              ><span>由 minicode 在本机执行</span>
            </div>
          </div>
        </section>

        <aside class="inspector" aria-label="执行状态">
          <section class="inspector-section">
            <div class="section-heading">
              <h2>运行状态</h2>
              <span class="status-badge" :class="{ running: active?.busy }">{{
                active?.pending_approvals.length
                  ? "等待批准"
                  : active?.busy
                    ? "执行中"
                    : "就绪"
              }}</span>
            </div>
            <p class="inspector-intro">
              {{
                active ? active.activity : "创建会话后，这里会显示实时状态。"
              }}
            </p>
            <dl class="metrics">
              <dt>模型调用</dt>
              <dd>{{ active?.metrics.counts?.model_calls ?? 0 }}</dd>
              <dt>工具调用</dt>
              <dd>{{ active?.metrics.counts?.tool_calls ?? 0 }}</dd>
              <dt>输入 token</dt>
              <dd>{{ number(active?.metrics.token_totals?.input_tokens) }}</dd>
              <dt>输出 token</dt>
              <dd>{{ number(active?.metrics.token_totals?.output_tokens) }}</dd>
            </dl>
          </section>
          <section class="inspector-section plan-section">
            <div class="section-heading">
              <h2>任务计划</h2>
              <span class="small-counter"
                >{{ completed }} / {{ active?.todos.length ?? 0 }}</span
              >
            </div>
            <ol v-if="active?.todos.length" class="todo-list">
              <li
                v-for="(todo, index) in active.todos"
                :key="index"
                :class="todo.status"
              >
                <PhCheck
                  v-if="todo.status === 'completed'"
                  :size="15"
                /><PhCircleNotch
                  v-else-if="todo.status === 'in_progress'"
                  :size="15"
                  class="spin"
                /><PhCircle v-else :size="15" /><span>{{ todo.content }}</span>
              </li>
            </ol>
            <div v-else class="plan-empty">
              <span class="plan-lines"><i></i><i></i><i></i></span>
              <p>任务开始后，<br />Agent 的计划会显示在这里。</p>
            </div>
          </section>
          <section class="inspector-section context-section">
            <div class="section-heading">
              <h2>上下文占用</h2>
              <span class="estimate-label">估计</span>
            </div>
            <div class="context-total">
              {{ usage ? number(usage.estimated_input_tokens) : "—"
              }}<span>token</span>
            </div>
            <div
              class="context-meter"
              role="progressbar"
              aria-label="上下文预算占用"
              :aria-valuenow="Math.round(occupied)"
              aria-valuemin="0"
              aria-valuemax="100"
            >
              <span
                :style="{ width: occupied + '%' }"
                :class="{ high: occupied >= 90 }"
              ></span>
            </div>
            <div class="budget-caption">
              <span>{{
                usage?.input_token_limit
                  ? `预算 ${number(usage.input_token_limit)}`
                  : usage
                    ? "自动压缩已关闭"
                    : "等待首轮请求"
              }}</span
              ><span v-if="usage?.input_token_limit"
                >{{ occupied.toFixed(1) }}%</span
              >
            </div>
            <dl class="metrics breakdown">
              <template v-for="[key, label] in breakdown" :key="key"
                ><dt>{{ label }}</dt>
                <dd>
                  {{
                    usage?.estimated_input_token_breakdown
                      ? number(usage.estimated_input_token_breakdown[key!])
                      : "—"
                  }}
                </dd></template
              >
            </dl>
            <p class="context-note">
              最近一次请求的输入估计，包含安全余量；实际用量以上方模型统计为准。
            </p>
          </section>
        </aside>
      </div>
    </main>

    <dialog
      v-if="showNew"
      ref="newDialog"
      class="modal new-modal"
      aria-labelledby="new-title"
      @cancel.prevent="!creating && (showNew = false)"
    >
      <div class="modal-title-row">
        <div>
          <p class="eyebrow">开始一段新的工作</p>
          <h2 id="new-title">创建会话</h2>
        </div>
        <button
          class="icon-button"
          aria-label="关闭新建会话"
          :disabled="creating"
          @click="showNew = false"
        >
          <PhX :size="21" />
        </button>
      </div>
      <form @submit.prevent="createSession">
        <label class="form-label" for="workdir">项目工作目录</label>
        <div class="input-with-icon">
          <PhFolder :size="18" /><input
            id="workdir"
            ref="workspaceInput"
            v-model="form.workdir"
            :disabled="pickingDirectory"
            required
            placeholder="D:\your-project"
            autocomplete="off"
          />
          <button
            type="button"
            class="folder-picker-button"
            :disabled="pickingDirectory || creating"
            @click="browseDirectory"
          >
            <PhCircleNotch v-if="pickingDirectory" :size="14" class="spin" />
            {{ pickingDirectory ? "选择中…" : "选择文件夹" }}
          </button>
        </div>
        <p class="field-help" role="status">
          {{
            pickingDirectory
              ? "请在系统弹出的窗口中选择项目文件夹。"
              : "选择本机文件夹，或直接填写已有项目的完整路径。"
          }}
        </p>
        <label class="form-label" for="model">使用模型</label
        ><select
          id="model"
          :value="form.mock ? 'mock' : 'configured'"
          @change="
            form.mock = ($event.target as HTMLSelectElement).value === 'mock'
          "
        >
          <option v-if="config?.provider !== 'mock'" value="configured">
            {{ config?.model }} · 当前配置
          </option>
          <option value="mock">Mock · 免费流程演示</option>
        </select>
        <fieldset class="mode-fieldset">
          <legend class="form-label">权限模式</legend>
          <div class="mode-options">
            <label
              v-for="option in config?.permission_modes"
              :key="option.value"
              :class="{ chosen: form.permission_mode === option.value }"
              ><input
                v-model="form.permission_mode"
                type="radio"
                name="permission-mode"
                :value="option.value" /><PhShieldCheck :size="18" /><strong>{{
                option.label
              }}</strong
              ><span>{{
                option.value === "read-only"
                  ? "查看文件与搜索"
                  : option.value === "workspace"
                    ? "在项目内读写与执行"
                    : "直接在宿主机执行"
              }}</span
              ><PhCheck
                v-if="form.permission_mode === option.value"
                class="mode-check"
                :size="15"
            /></label>
          </div>
        </fieldset>
        <p
          class="mode-explanation"
          :class="{ caution: form.permission_mode === 'full' }"
        >
          {{
            form.permission_mode === "full"
              ? "完全权限使用当前系统用户的权限，不受 Docker 文件隔离保护。"
              : "命令通过 Docker 隔离；文件工具在本机检查权限。工作区外访问需批准。"
          }}
        </p>
        <p
          v-if="form.mock && form.permission_mode !== 'full'"
          class="field-help"
        >
          Mock 的目录查询也使用真实工具，当前模式需要
          Docker。只验证页面流程可选择完全权限。
        </p>
        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
        <div class="modal-actions">
          <button
            type="button"
            class="button secondary"
            :disabled="creating"
            @click="showNew = false"
          >
            取消</button
          ><button
            type="submit"
            class="button primary"
            :disabled="creating || pickingDirectory || !form.workdir.trim()"
          >
            {{ creating ? "正在创建…" : "创建会话" }}<PhArrowRight :size="16" />
          </button>
        </div>
      </form>
    </dialog>
    <ApprovalDialog
      v-if="currentApproval"
      :key="currentApproval.id"
      :request="currentApproval"
      :waiting="approvalWaiting"
      @decide="decide"
    />
    <SessionSettingsDialog
      v-if="settingsKind && active && config"
      :key="active.metadata.id + settingsKind"
      :session="active"
      :config="config"
      :kind="settingsKind"
      @close="settingsKind = undefined"
    />
  </div>
</template>
