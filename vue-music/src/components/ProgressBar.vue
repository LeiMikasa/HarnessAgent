<script setup>
/**
 * 播放进度条：纯展示组件（CONTRACT.md §2.3）。
 * 拖动 range → emit('seek', 目标秒数)。
 */
import { computed } from 'vue'
import { formatPercent, formatTime } from '../utils/format.js'

const props = defineProps({
  currentTime: { type: Number, default: 0 },
  duration: { type: Number, default: 0 },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['seek'])
const percent = computed(() => formatPercent(props.currentTime, props.duration))
const isDisabled = computed(() => props.disabled || props.duration <= 0)
</script>

<template>
  <div class="progress-bar" data-test="progress-bar" :data-disabled="String(isDisabled)">
    <span data-test="progress-current">{{ formatTime(props.currentTime) }}</span>
    <div class="progress-rail">
      <div data-test="progress-fill" :style="{ width: percent + '%' }" />
      <input
        data-test="progress-range"
        type="range"
        min="0"
        max="100"
        step="0.1"
        :disabled="isDisabled"
        :value="percent"
        aria-label="播放进度"
        @input="emit('seek', (Number($event.target.value) / 100) * props.duration)"
      />
    </div>
    <span data-test="progress-duration">{{ formatTime(props.duration) }}</span>
  </div>
</template>

<style scoped>
[data-test='progress-bar'] {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 12px;
  align-items: center;
  width: 100%;
}

[data-test='progress-current'],
[data-test='progress-duration'] {
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.progress-rail {
  position: relative;
  display: flex;
  align-items: center;
  height: 20px;
}

/* 轨道底槽 */
.progress-rail::before {
  content: '';
  position: absolute;
  right: 0;
  left: 0;
  height: 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
}

/* 已播部分：绘制在底槽之上、range 之下 */
[data-test='progress-fill'] {
  position: absolute;
  left: 0;
  height: 6px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  pointer-events: none;
  transition: width 0.12s linear;
}

[data-test='progress-range'] {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 20px;
  margin: 0;
  background: transparent;
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
}

[data-test='progress-range']::-webkit-slider-runnable-track {
  height: 6px;
  background: transparent;
}

[data-test='progress-range']::-webkit-slider-thumb {
  width: 14px;
  height: 14px;
  margin-top: -4px;
  border: 2px solid #fff;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 2px 6px rgba(4, 8, 24, 0.6);
  appearance: none;
  -webkit-appearance: none;
}

[data-test='progress-range']::-moz-range-track {
  height: 6px;
  background: transparent;
}

[data-test='progress-range']::-moz-range-thumb {
  width: 12px;
  height: 12px;
  border: 2px solid #fff;
  border-radius: 50%;
  background: var(--accent);
}

[data-test='progress-range']:disabled {
  cursor: not-allowed;
}

[data-test='progress-bar'][data-disabled='true'] {
  opacity: 0.55;
}

[data-test='progress-bar'][data-disabled='true'] .progress-rail::before {
  background: rgba(255, 255, 255, 0.08);
}
</style>
