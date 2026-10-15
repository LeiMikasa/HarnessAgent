<script setup>
/**
 * 播放条：纯展示组件（CONTRACT.md §2.5）。
 * 自身不接触 <audio>，所有交互都往 App.vue 抛事件。
 */
import { computed } from 'vue'
import ProgressBar from './ProgressBar.vue'
import VolumeControl from './VolumeControl.vue'

const props = defineProps({
  track: { type: Object, default: null },
  isPlaying: { type: Boolean, default: false },
  currentTime: { type: Number, default: 0 },
  duration: { type: Number, default: 0 },
  volume: { type: Number, default: 1 },
  muted: { type: Boolean, default: false },
  shuffle: { type: Boolean, default: false },
  repeat: { type: String, default: 'off' },
})
const emit = defineEmits([
  'toggle-play',
  'prev',
  'next',
  'seek',
  'volume',
  'toggle-mute',
  'toggle-shuffle',
  'cycle-repeat',
])

const REPEAT_LABEL = { off: '顺序播放', all: '列表循环', one: '单曲循环' }
const repeatLabel = computed(() => REPEAT_LABEL[props.repeat] ?? REPEAT_LABEL.off)
</script>

<template>
  <div class="player-bar" data-test="player-bar">
    <span data-test="now-playing-title">{{ props.track ? props.track.title : '未选择歌曲' }}</span>
    <span data-test="now-playing-artist">{{ props.track ? props.track.artist : '—' }}</span>
    <button
      data-test="shuffle"
      class="icon-btn"
      :aria-pressed="String(props.shuffle)"
      aria-label="随机播放"
      @click="emit('toggle-shuffle')"
    >
      🔀
    </button>
    <button class="icon-btn" data-test="prev" aria-label="上一首" @click="emit('prev')">⏮</button>
    <button
      data-test="play-toggle"
      class="play-btn"
      :aria-label="props.isPlaying ? '暂停' : '播放'"
      @click="emit('toggle-play')"
    >
      {{ props.isPlaying ? '❚❚' : '▶' }}
    </button>
    <button class="icon-btn" data-test="next" aria-label="下一首" @click="emit('next')">⏭</button>
    <button
      data-test="repeat"
      class="icon-btn"
      :data-repeat="props.repeat"
      :aria-label="repeatLabel"
      @click="emit('cycle-repeat')"
    >
      🔁
    </button>
    <ProgressBar
      :current-time="props.currentTime"
      :duration="props.duration"
      @seek="emit('seek', $event)"
    />
    <VolumeControl
      :volume="props.volume"
      :muted="props.muted"
      @update:volume="emit('volume', $event)"
      @toggle-mute="emit('toggle-mute')"
    />
  </div>
</template>

<style scoped>
.player-bar {
  position: fixed;
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 30;
  display: flex;
  flex-wrap: wrap;
  gap: 10px 12px;
  align-items: center;
  padding: 12px 22px 14px;
  border-top: 1px solid var(--border);
  background: rgba(11, 16, 32, 0.86);
  box-shadow: 0 -14px 40px rgba(4, 8, 24, 0.5);
  backdrop-filter: blur(18px);
}

[data-test='now-playing-title'] {
  overflow: hidden;
  max-width: 240px;
  font-size: 14px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

[data-test='now-playing-artist'] {
  overflow: hidden;
  max-width: 180px;
  color: var(--muted);
  font-size: 12.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 传输控制按钮右对齐 */
[data-test='shuffle'] {
  margin-left: auto;
}

.icon-btn {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--text);
  font-size: 15px;
  transition:
    background 0.16s ease,
    color 0.16s ease;
}

.icon-btn:hover {
  background: var(--panel-strong);
}

[data-test='shuffle'][aria-pressed='true'],
[data-test='repeat'][data-repeat='all'],
[data-test='repeat'][data-repeat='one'] {
  color: var(--accent-2);
}

.play-btn {
  display: grid;
  place-items: center;
  width: 46px;
  height: 46px;
  border: 0;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff;
  font-size: 15px;
  box-shadow: 0 8px 20px rgba(139, 92, 246, 0.45);
  transition: transform 0.16s ease;
}

.play-btn:hover {
  transform: scale(1.06);
}

.play-btn:active {
  transform: scale(0.97);
}

/* 单曲循环时在循环按钮右上角加一个小「1」角标 */
[data-test='repeat'] {
  position: relative;
}

[data-test='repeat'][data-repeat='one']::after {
  content: '1';
  position: absolute;
  top: 1px;
  right: 1px;
  padding: 0 3px;
  border-radius: 6px;
  background: var(--accent);
  color: #fff;
  font-size: 9px;
  line-height: 12px;
  font-weight: 700;
}

/* 进度条与音量放到第二行 */
[data-test='progress-bar'] {
  flex: 1 1 320px;
  min-width: 0;
  order: 10;
}

[data-test='volume-control'] {
  order: 11;
  flex: 0 1 210px;
}

@media (max-width: 640px) {
  .player-bar {
    gap: 8px;
    padding: 10px 14px 12px;
  }

  [data-test='now-playing-artist'] {
    max-width: 100px;
  }

  .icon-btn {
    width: 30px;
    height: 30px;
    font-size: 14px;
  }

  .play-btn {
    width: 42px;
    height: 42px;
  }

  [data-test='volume-control'] {
    flex: 1 1 120px;
  }
}
</style>
