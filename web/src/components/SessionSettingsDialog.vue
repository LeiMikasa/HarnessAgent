<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { PhX } from "@phosphor-icons/vue";
import { api } from "../api";
import { modeLabels } from "../types";
import type { Config, Mode, Session } from "../types";

const props = defineProps<{
  session: Session;
  config: Config;
  kind: "permission" | "model";
}>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement>();
const cancel = ref<HTMLButtonElement>();
const saving = ref(false);
const error = ref("");
const mode = ref<Mode>(
  props.session.pending_settings?.permission_mode ??
    props.session.metadata.permission_mode,
);
const provider = ref(
  props.session.pending_settings?.provider ?? props.session.metadata.provider,
);
const model = ref(
  props.session.pending_settings?.model ?? props.session.metadata.model,
);
const confirmed = ref(false);
const needsFullConfirmation = computed(
  () =>
    mode.value === "full" && props.session.metadata.permission_mode !== "full",
);
const providers = computed(() => [...new Set([props.config.provider, "mock"])]);
let previous: HTMLElement | null = null;
watch(provider, (value) => {
  model.value = value === "mock" ? "mock-1" : props.config.model;
});
watch(mode, () => {
  confirmed.value = false;
});
onMounted(async () => {
  previous = document.activeElement as HTMLElement;
  dialog.value?.showModal();
  await nextTick();
  cancel.value?.focus();
});
onBeforeUnmount(() => {
  dialog.value?.close();
  previous?.focus();
});
async function save() {
  if (saving.value) return;
  saving.value = true;
  error.value = "";
  try {
    const payload =
      props.kind === "permission"
        ? { permission_mode: mode.value, confirm_full: confirmed.value }
        : { provider: provider.value, model: model.value.trim() };
    await api(`/sessions/${props.session.metadata.id}/settings`, payload);
    emit("close");
  } catch (value) {
    error.value = value instanceof Error ? value.message : String(value);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <dialog
    ref="dialog"
    class="modal new-modal"
    aria-labelledby="settings-title"
    @cancel.prevent="!saving && emit('close')"
  >
    <form @submit.prevent="save">
      <div class="modal-top">
        <p class="eyebrow">当前会话</p>
        <button
          type="button"
          class="icon-button"
          aria-label="关闭设置"
          :disabled="saving"
          @click="emit('close')"
        >
          <PhX :size="20" />
        </button>
      </div>
      <h2 id="settings-title">
        {{ kind === "permission" ? "调整权限" : "切换模型" }}
      </h2>
      <p class="modal-description">
        保留当前对话。正在执行的调用先完成，新设置在下一次调用前生效。
      </p>
      <template v-if="kind === 'permission'">
        <fieldset class="mode-fieldset">
          <legend class="form-label">权限模式</legend>
          <div class="mode-options">
            <label
              v-for="(label, value) in modeLabels"
              :key="value"
              class="mode-option"
              :class="{ chosen: mode === value }"
            >
              <input
                v-model="mode"
                type="radio"
                name="session-permission"
                :value="value"
                :disabled="saving"
              />
              <strong>{{ label }}</strong
              ><span>{{
                value === "read-only"
                  ? "查看文件与搜索"
                  : value === "workspace"
                    ? "在项目内读写与执行"
                    : "直接在宿主机执行"
              }}</span>
            </label>
          </div>
        </fieldset>
        <p class="mode-explanation" :class="{ caution: mode === 'full' }">
          {{
            mode === "full"
              ? "本会话后续工具将在宿主机执行，直到再次切换权限。"
              : "命令通过 Docker 隔离，文件工具在本机检查权限；切换时关闭旧容器，容器后台进程将结束。"
          }}
        </p>
        <label v-if="needsFullConfirmation" class="settings-confirm"
          ><input
            v-model="confirmed"
            type="checkbox"
            :disabled="saving"
          />我确认将本会话切换为完全权限</label
        >
        <p class="field-help">
          切换权限会拒绝尚未完成的旧审批；当前已执行的操作不会撤销。
        </p>
      </template>
      <template v-else>
        <label class="form-label" for="session-provider">模型服务</label>
        <select id="session-provider" v-model="provider" :disabled="saving">
          <option v-for="item in providers" :key="item" :value="item">
            {{
              item === "mock" ? "Mock · 免费演示" : item + " · 当前配置的连接"
            }}
          </option>
        </select>
        <label class="form-label" for="session-model">模型 ID</label>
        <input
          id="session-model"
          class="settings-model-input"
          v-model="model"
          maxlength="200"
          required
          :disabled="saving"
          list="configured-models"
          autocomplete="off"
        />
        <datalist id="configured-models">
          <option :value="provider === 'mock' ? 'mock-1' : config.model" />
        </datalist>
        <p class="field-help">
          {{
            provider === "mock"
              ? "Mock 是预设演示，不会调用真实模型。"
              : "使用当前服务已配置的地址和密钥。可填写该服务支持的模型 ID；实际可用性由下一次请求验证，调用按服务计费。"
          }}
        </p>
      </template>
      <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
      <div class="modal-actions">
        <button
          ref="cancel"
          type="button"
          class="button secondary"
          :disabled="saving"
          @click="emit('close')"
        >
          取消</button
        ><button
          type="submit"
          class="button primary"
          :disabled="
            saving ||
            (kind === 'permission' && needsFullConfirmation && !confirmed) ||
            (kind === 'model' && !model.trim())
          "
        >
          {{ saving ? "正在提交…" : "应用设置" }}
        </button>
      </div>
    </form>
  </dialog>
</template>
