<script setup>
/**
 * 曲目列表：纯展示组件（CONTRACT.md §2.2）。
 * 点击行 → emit select(id)；点收藏 → emit toggle-favorite(id)。
 */
import { formatTime } from '../utils/format.js'

const props = defineProps({
  tracks: { type: Array, default: () => [] },
  currentId: { type: String, default: null },
  isPlaying: { type: Boolean, default: false },
  favorites: { type: Array, default: () => [] },
})
const emit = defineEmits(['select', 'toggle-favorite'])
</script>

<template>
  <ul class="track-list" data-test="track-list">
    <li v-if="!props.tracks.length" data-test="track-list-empty">没有匹配的歌曲</li>
    <li
      v-for="track in props.tracks"
      :key="track.id"
      data-test="track-item"
      :data-track-id="track.id"
      :data-active="String(track.id === props.currentId)"
      :data-playing="String(track.id === props.currentId && props.isPlaying)"
    >
      <div class="track-main" @click="emit('select', track.id)">
        <span
          data-test="track-cover"
          class="track-cover"
          :style="{ background: track.color || 'var(--panel)' }"
          >{{ track.cover }}</span
        >
        <span data-test="track-title">{{ track.title }}</span>
        <span data-test="track-artist">{{ track.artist }}</span>
        <span data-test="track-duration">{{ formatTime(track.duration) }}</span>
      </div>
      <button
        data-test="favorite-btn"
        :data-track-id="track.id"
        :data-active="String(props.favorites.includes(track.id))"
        :aria-pressed="String(props.favorites.includes(track.id))"
        aria-label="收藏"
        @click.stop="emit('toggle-favorite', track.id)"
      >
        ♥
      </button>
    </li>
  </ul>
</template>

<style scoped>
[data-test='track-list'] {
  margin: 0;
  padding: 6px;
  list-style: none;
}

li[data-test='track-item'] {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px 2px 6px;
  border-left: 3px solid transparent;
  border-radius: var(--radius);
  transition:
    background 0.16s ease,
    border-color 0.16s ease;
}

li[data-test='track-item'] + li[data-test='track-item'] {
  margin-top: 2px;
}

li[data-test='track-item']:hover {
  background: var(--panel);
}

li[data-test='track-item'][data-active='true'] {
  border-left-color: var(--accent);
  background: var(--accent-soft);
}

.track-main {
  display: grid;
  flex: 1;
  grid-template-columns: 44px minmax(0, 1.5fr) minmax(0, 1fr) 54px;
  gap: 12px;
  align-items: center;
  min-width: 0;
  padding: 8px 4px;
  cursor: pointer;
}

[data-test='track-cover'] {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  font-size: 20px;
  border-radius: 12px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.12);
}

[data-test='track-title'] {
  overflow: hidden;
  font-size: 14.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

li[data-test='track-item'][data-active='true'] [data-test='track-title'] {
  font-weight: 600;
}

[data-test='track-artist'] {
  overflow: hidden;
  color: var(--muted);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

[data-test='track-duration'] {
  color: var(--muted);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

[data-test='favorite-btn'] {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--muted);
  font-size: 15px;
  transition: 0.16s ease;
}

[data-test='favorite-btn']:hover {
  background: var(--panel-strong);
  color: #f9a8d4;
}

[data-test='favorite-btn'][data-active='true'] {
  color: #f472b6;
  text-shadow: 0 0 12px rgba(244, 114, 182, 0.6);
}

[data-test='track-list-empty'] {
  padding: 48px 0;
  color: var(--muted);
  font-size: 14px;
  text-align: center;
}

/* 正在播放的行：封面轻微脉动，作为"当前曲目"的视觉提示 */
li[data-test='track-item'][data-playing='true'] [data-test='track-cover'] {
  animation: cover-pulse 1.8s ease-in-out infinite;
}

@keyframes cover-pulse {
  0%,
  100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.07);
  }
}

@media (max-width: 640px) {
  .track-main {
    grid-template-columns: 40px minmax(0, 1fr) 48px;
    gap: 10px;
  }

  [data-test='track-artist'] {
    display: none;
  }

  [data-test='track-cover'] {
    width: 40px;
    height: 40px;
    font-size: 18px;
  }
}
</style>
