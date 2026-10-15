<script setup>
/**
 * 音量控制：纯展示组件（CONTRACT.md §2.4）。
 * 拖动 range → emit('update:volume', 0~1)；点喇叭 → emit('toggle-mute')。
 */
import { formatVolume } from '../utils/format.js'

const props = defineProps({
  volume: { type: Number, default: 1 },
  muted: { type: Boolean, default: false },
})
const emit = defineEmits(['update:volume', 'toggle-mute'])
</script>

<template>
  <div class="volume-control" data-test="volume-control" :data-muted="String(props.muted)">
    <button
      data-test="volume-mute"
      :aria-pressed="String(props.muted)"
      aria-label="静音开关"
      @click="emit('toggle-mute')"
    >
      {{ props.muted ? '🔇' : '🔊' }}
    </button>
    <input
      data-test="volume-range"
      type="range"
      min="0"
      max="100"
      step="1"
      :value="Math.round(props.volume * 100)"
      aria-label="音量"
      @input="emit('update:volume', Number($event.target.value) / 100)"
    />
    <span data-test="volume-value">{{ props.muted ? '静音' : formatVolume(props.volume) }}</span>
  </div>
</template>

<style scoped>
[data-test='volume-control'] {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 168px;
}

[data-test='volume-mute'] {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel);
  font-size: 14px;
  transition: 0.16s ease;
}

[data-test='volume-mute']:hover {
  background: var(--panel-strong);
}

[data-test='volume-control'][data-muted='true'] [data-test='volume-mute'] {
  border-color: rgba(248, 113, 113, 0.45);
  color: var(--danger);
}

[data-test='volume-range'] {
  flex: 1;
  min-width: 80px;
  height: 20px;
  margin: 0;
  background: transparent;
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
}

[data-test='volume-range']::-webkit-slider-runnable-track {
  height: 5px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
}

[data-test='volume-range']::-webkit-slider-thumb {
  width: 12px;
  height: 12px;
  margin-top: -3.5px;
  border: 0;
  border-radius: 50%;
  background: var(--accent-2);
  appearance: none;
  -webkit-appearance: none;
}

[data-test='volume-range']::-moz-range-track {
  height: 5px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
}

[data-test='volume-range']::-moz-range-thumb {
  width: 12px;
  height: 12px;
  border: 0;
  border-radius: 50%;
  background: var(--accent-2);
}

[data-test='volume-value'] {
  min-width: 38px;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

@media (max-width: 640px) {
  [data-test='volume-control'] {
    min-width: 120px;
  }

  [data-test='volume-value'] {
    display: none;
  }
}
</style>
