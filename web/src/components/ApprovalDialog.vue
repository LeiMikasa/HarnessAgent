<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from "vue";
import { PhShieldWarning, PhX } from "@phosphor-icons/vue";
import type { Approval } from "../types";

defineProps<{ request: Approval; waiting: boolean }>();
const emit = defineEmits<{ decide: [allowed: boolean] }>();
const dialog = ref<HTMLDialogElement>();
const deny = ref<HTMLButtonElement>();
let previous: HTMLElement | null = null;
onMounted(async () => {
  previous = document.activeElement as HTMLElement;
  dialog.value?.showModal();
  await nextTick();
  deny.value?.focus();
});
onBeforeUnmount(() => {
  dialog.value?.close();
  previous?.focus();
});
</script>

<template>
  <dialog
    ref="dialog"
    class="modal approval-modal"
    aria-labelledby="approval-title"
    @cancel.prevent="!waiting && emit('decide', false)"
  >
    <div class="modal-top">
      <span class="dialog-icon"><PhShieldWarning :size="23" /></span
      ><button
        class="icon-button"
        aria-label="拒绝本次操作"
        :disabled="waiting"
        @click="emit('decide', false)"
      >
        <PhX :size="20" />
      </button>
    </div>
    <p class="eyebrow">需要你的确认</p>
    <h2 id="approval-title">
      {{
        request.host_execution ? "允许本次在宿主机执行？" : "允许这次工具操作？"
      }}
    </h2>
    <p class="modal-description">
      {{
        request.host_execution
          ? "这一次操作将绕过 Docker 隔离。执行后恢复原来的权限模式。"
          : "Agent 正在等待你的决定，批准只对当前操作生效。"
      }}
    </p>
    <div class="approval-details">
      <dl>
        <dt>工具</dt>
        <dd>{{ request.tool }}</dd>
        <dt>调用者</dt>
        <dd>{{ request.owner }}</dd>
        <dt>目录</dt>
        <dd>{{ request.workdir }}</dd>
      </dl>
      <p class="approval-reason">{{ request.reason }}</p>
      <span class="field-label">完整调用参数</span>
      <pre>{{ JSON.stringify(request.arguments, null, 2) }}</pre>
    </div>
    <div class="modal-actions">
      <button
        ref="deny"
        class="button secondary"
        :disabled="waiting"
        @click="emit('decide', false)"
      >
        拒绝本次</button
      ><button
        class="button primary"
        :disabled="waiting"
        @click="emit('decide', true)"
      >
        {{ waiting ? "正在提交…" : "允许本次操作" }}
      </button>
    </div>
  </dialog>
</template>
