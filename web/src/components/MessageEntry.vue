<script setup lang="ts">
import { computed } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import {
  PhTerminalWindow,
  PhCheckCircle,
  PhXCircle,
  PhCircleNotch,
  PhCaretRight,
  PhBrain,
} from "@phosphor-icons/vue";
import type { Entry } from "../types";

const props = defineProps<{ entry: Entry }>();
const rendered = computed(() =>
  DOMPurify.sanitize(marked.parse(props.entry.text || "", { async: false }), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["img"],
  }),
);
const toolTarget = computed(() => {
  const args = props.entry.arguments ?? {};
  return String(args.path ?? args.command ?? args.pattern ?? "");
});
</script>

<template>
  <details
    v-if="entry.kind === 'tool'"
    class="tool-entry"
    :class="entry.status"
    :open="entry.status === 'failed'"
  >
    <summary>
      <PhCircleNotch
        v-if="entry.status === 'running'"
        class="spin tool-state"
        :size="17"
      />
      <PhCheckCircle
        v-else-if="entry.status === 'done'"
        class="tool-state"
        :size="17"
      />
      <PhXCircle v-else class="tool-state" :size="17" />
      <PhTerminalWindow :size="17" />
      <span class="tool-name">{{ entry.name }}</span>
      <span class="tool-target">{{ toolTarget }}</span>
      <span v-if="entry.elapsed !== undefined" class="tool-time"
        >{{ entry.elapsed.toFixed(1) }}s</span
      >
      <PhCaretRight :size="13" class="disclosure" />
    </summary>
    <div class="tool-body">
      <span class="field-label">调用参数</span>
      <pre>{{ JSON.stringify(entry.arguments, null, 2) }}</pre>
      <template v-if="entry.text">
        <span class="field-label">执行结果</span>
        <pre>{{ entry.text }}</pre>
        <p
          v-if="entry.result_chars && entry.result_chars > entry.text.length"
          class="muted"
        >
          界面显示部分结果，工具实际返回
          {{ entry.result_chars.toLocaleString() }} 字符。
        </p>
      </template>
    </div>
  </details>
  <article v-else class="message" :class="[entry.role, entry.kind]">
    <div v-if="entry.role === 'assistant'" class="message-author">
      <span class="mini-mark">m</span>minicode<span
        v-if="entry.owner && entry.owner !== 'lead'"
      >
        / {{ entry.owner }}</span
      >
    </div>
    <details
      v-if="entry.role === 'assistant' && entry.thinking"
      class="thinking-entry"
    >
      <summary>
        <PhCircleNotch
          v-if="entry.thinking_status === 'streaming'"
          class="spin"
          :size="15"
        />
        <PhBrain v-else :size="15" />
        <span>思考过程</span>
        <span
          v-if="entry.thinking_status === 'streaming'"
          class="thinking-status"
          >生成中…</span
        >
        <span
          v-else-if="entry.thinking_status === 'interrupted'"
          class="thinking-status"
          >已中断</span
        >
        <PhCaretRight :size="13" class="disclosure" />
      </summary>
      <pre class="thinking-body">{{ entry.thinking }}</pre>
    </details>
    <div
      v-if="entry.role === 'assistant' && entry.text"
      class="prose"
      v-html="rendered"
    ></div>
    <p v-else class="plain-message">{{ entry.text }}</p>
    <span v-if="entry.status === 'interrupted'" class="interrupted"
      >此段输出未完成</span
    >
  </article>
</template>
