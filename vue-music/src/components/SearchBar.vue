<script setup>
/**
 * 搜索栏：纯展示组件（CONTRACT.md §2.1）。
 * 只负责把输入透出成 `update:modelValue`，过滤逻辑在 player 里。
 */
import { ref } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  resultCount: { type: Number, default: 0 },
  totalCount: { type: Number, default: 0 },
})
const emit = defineEmits(['update:modelValue', 'clear'])
const inputRef = ref(null)
function focus() {
  inputRef.value?.focus()
}
defineExpose({ focus })
</script>

<template>
  <div
    class="search-bar"
    data-test="search-bar"
    :data-filtered="String(props.resultCount < props.totalCount)"
  >
    <span class="search-icon" aria-hidden="true">🔍</span>
    <input
      ref="inputRef"
      data-test="search-input"
      type="search"
      :value="props.modelValue"
      placeholder="搜索歌曲 / 歌手 / 专辑"
      aria-label="搜索歌曲"
      @input="emit('update:modelValue', $event.target.value)"
    />
    <button
      v-if="props.modelValue"
      data-test="search-clear"
      aria-label="清除搜索"
      @click="emit('clear')"
    >
      ✕
    </button>
    <span data-test="search-count">显示 {{ props.resultCount }} / {{ props.totalCount }} 首</span>
  </div>
</template>

<style scoped>
.search-bar {
  display: flex;
  flex: 1 1 300px;
  align-items: center;
  gap: 8px;
  max-width: 520px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel);
  transition:
    border-color 0.18s ease,
    box-shadow 0.18s ease;
}

.search-bar:hover {
  border-color: rgba(255, 255, 255, 0.24);
}

.search-bar:focus-within {
  border-color: transparent;
  box-shadow: 0 0 0 2px var(--accent);
}

.search-bar[data-filtered='true'] [data-test='search-count'] {
  color: var(--accent-2);
}

.search-icon {
  font-size: 13px;
  line-height: 1;
  opacity: 0.7;
}

[data-test='search-input'] {
  flex: 1;
  min-width: 0;
  padding: 5px 0;
  border: 0;
  background: transparent;
  color: var(--text);
  font-size: 14px;
  outline: none;
}

[data-test='search-input']::placeholder {
  color: var(--muted);
}

/* 去掉 search 类型输入框自带的清除叉，避免和自定义按钮重复 */
[data-test='search-input']::-webkit-search-cancel-button {
  appearance: none;
}

[data-test='search-clear'] {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border: 0;
  border-radius: 50%;
  background: var(--panel-strong);
  color: var(--muted);
  font-size: 11px;
  transition: 0.16s ease;
}

[data-test='search-clear']:hover {
  color: #fff;
  background: var(--accent);
}

[data-test='search-count'] {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}

@media (max-width: 640px) {
  [data-test='search-count'] {
    display: none;
  }
}
</style>
