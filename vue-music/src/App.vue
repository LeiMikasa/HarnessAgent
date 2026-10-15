<script setup>
/**
 * LZX 音乐台 · Vue 版 —— 页面装配层。
 * 所有子组件都是纯展示组件，这里负责把 player 的状态/动作与它们对接，
 * 并处理全局键盘快捷键。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { usePlayer } from './composables/usePlayer.js'
import { formatTotalDuration } from './utils/format.js'
import SearchBar from './components/SearchBar.vue'
import TrackList from './components/TrackList.vue'
import PlayerBar from './components/PlayerBar.vue'

const player = usePlayer()
const state = player.state
const searchRef = ref(null)

const currentId = computed(() => player.currentTrack?.id ?? null)
const listSummary = computed(() => {
  const total = formatTotalDuration(player.visibleTracks)
  return `${player.visibleTracks.length} 首 · 共 ${total}`
})
const repeatLabels = { off: '顺序播放', all: '列表循环', one: '单曲循环' }

function handleSelect(id) {
  player.selectTrack(id)
}

function handleKeydown(event) {
  const target = event.target
  const typing =
    !!target &&
    (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable === true)

  if (event.key === '/' && !typing) {
    event.preventDefault()
    searchRef.value?.focus?.()
    return
  }
  if (event.key === 'Escape') {
    player.clearQuery()
    if (typing) target.blur?.()
    return
  }
  if (typing) return

  switch (event.key) {
    case ' ':
    case 'Spacebar':
      event.preventDefault()
      player.togglePlay()
      break
    case 'ArrowRight':
      player.next()
      break
    case 'ArrowLeft':
      player.prev()
      break
    case 'ArrowUp':
      event.preventDefault()
      player.setVolume(state.volume + 0.05)
      break
    case 'ArrowDown':
      event.preventDefault()
      player.setVolume(state.volume - 0.05)
      break
    case 'm':
    case 'M':
      player.toggleMute()
      break
    case 's':
    case 'S':
      player.toggleShuffle()
      break
    case 'r':
    case 'R':
      player.cycleRepeat()
      break
    default:
      break
  }
}

onMounted(() => window.addEventListener('keydown', handleKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', handleKeydown))
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="brand">
        <span class="brand-logo" aria-hidden="true">🎵</span>
        <div>
          <h1 data-test="app-title">LZX 音乐台</h1>
          <p class="brand-sub">一万二司空震 的私人歌单 · Vue 3 + Vite</p>
        </div>
      </div>
      <div class="header-meta">
        <span class="pill" data-test="list-summary">{{ listSummary }}</span>
        <button
          class="ghost-btn"
          data-test="favorites-only"
          :data-active="String(state.favoritesOnly)"
          :aria-pressed="String(state.favoritesOnly)"
          @click="player.toggleFavoritesOnly()"
        >
          ♥ 只看收藏 ({{ state.favorites.length }})
        </button>
      </div>
    </header>

    <section class="toolbar">
      <SearchBar
        ref="searchRef"
        :model-value="state.query"
        :result-count="player.visibleTracks.length"
        :total-count="state.tracks.length"
        @update:model-value="player.setQuery"
        @clear="player.clearQuery"
      />
      <div class="mode-switch">
        <button
          class="ghost-btn"
          data-test="shuffle-toggle"
          :data-active="String(state.shuffle)"
          :aria-pressed="String(state.shuffle)"
          @click="player.toggleShuffle()"
        >
          🔀 随机
        </button>
        <button
          class="ghost-btn"
          data-test="repeat-toggle"
          :data-repeat="state.repeat"
          @click="player.cycleRepeat()"
        >
          🔁 {{ repeatLabels[state.repeat] }}
        </button>
      </div>
    </section>

    <p v-if="state.error" class="error-banner" data-test="error-banner" role="alert">
      播放出错：{{ state.error }}
    </p>

    <main class="playlist-area">
      <TrackList
        :tracks="player.visibleTracks"
        :current-id="currentId"
        :is-playing="state.isPlaying"
        :favorites="state.favorites"
        @select="handleSelect"
        @toggle-favorite="player.toggleFavorite"
      />
    </main>

    <p class="hotkeys">
      快捷键：<kbd>空格</kbd> 播放/暂停 · <kbd>←</kbd>/<kbd>→</kbd> 上/下一首 ·
      <kbd>↑</kbd>/<kbd>↓</kbd> 音量 · <kbd>M</kbd> 静音 · <kbd>S</kbd> 随机 ·
      <kbd>R</kbd> 循环 · <kbd>/</kbd> 搜索 · <kbd>Esc</kbd> 清空搜索
    </p>

    <PlayerBar
      :track="player.currentTrack"
      :is-playing="state.isPlaying"
      :current-time="state.currentTime"
      :duration="state.duration"
      :volume="state.volume"
      :muted="state.muted"
      :shuffle="state.shuffle"
      :repeat="state.repeat"
      @toggle-play="player.togglePlay()"
      @prev="player.prev()"
      @next="player.next()"
      @seek="player.seek"
      @volume="player.setVolume"
      @toggle-mute="player.toggleMute()"
      @toggle-shuffle="player.toggleShuffle()"
      @cycle-repeat="player.cycleRepeat()"
    />
  </div>
</template>

<style scoped>
.app-shell {
  max-width: 1080px;
  margin: 0 auto;
  padding: 32px 20px 200px;
}

.app-header {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
}

.brand {
  display: flex;
  align-items: center;
  gap: 14px;
}

.brand-logo {
  display: grid;
  place-items: center;
  width: 54px;
  height: 54px;
  font-size: 26px;
  border-radius: var(--radius);
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  box-shadow: var(--shadow);
}

.app-header h1 {
  margin: 0;
  font-size: 26px;
  letter-spacing: 0.5px;
}

.brand-sub {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 13px;
}

.header-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.pill {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel);
}

.ghost-btn {
  padding: 8px 14px;
  font-size: 13px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--panel);
  transition: 0.18s ease;
}

.ghost-btn:hover {
  background: var(--panel-strong);
}

.ghost-btn[data-active='true'] {
  border-color: transparent;
  color: #fff;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  margin: 26px 0 14px;
}

.mode-switch {
  display: flex;
  gap: 8px;
}

.error-banner {
  margin: 0 0 14px;
  padding: 10px 14px;
  font-size: 13px;
  color: #fff;
  border-radius: var(--radius-sm);
  background: rgba(248, 113, 113, 0.22);
  border: 1px solid rgba(248, 113, 113, 0.4);
}

.playlist-area {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--panel);
  backdrop-filter: blur(12px);
  overflow: hidden;
}

.hotkeys {
  margin: 16px 2px 0;
  font-size: 12px;
  color: var(--muted);
}

.hotkeys kbd {
  padding: 2px 6px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.08);
  font-size: 11px;
}

@media (max-width: 640px) {
  .app-shell {
    padding: 22px 14px 220px;
  }

  .app-header h1 {
    font-size: 21px;
  }
}
</style>
