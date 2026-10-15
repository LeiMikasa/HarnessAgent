/**
 * 播放器核心（状态 + 行为）。
 *
 * 设计要点：
 * 1. 逻辑与 UI 解耦：组件全部是纯展示组件，所有状态从这里下发。
 * 2. 逻辑与真实音频解耦：音频通过注入的 engine 访问，测试可注入内存假引擎。
 * 3. 返回值是 reactive 对象，模板里 `player.currentTrack` / `player.progress`
 *    会自动解包 computed，不需要再 `.value`。
 */

import { computed, reactive } from 'vue'
import { createAudioEngine } from '../utils/audio.js'
import {
  clamp01,
  cycleRepeatMode,
  filterFavorites,
  filterTracks,
  indexOfId,
  nextIndex,
  prevIndex,
} from '../utils/playlist.js'
import { tracks as defaultTracks } from '../data/tracks.js'

export const FAVORITES_STORAGE_KEY = 'lzx-music:favorites'

/** 取真实 localStorage；不可用时退化为内存实现（隐私模式 / 测试环境） */
export function createSafeStorage() {
  try {
    const probe = '__probe__'
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(probe, '1')
      localStorage.removeItem(probe)
      return localStorage
    }
  } catch {
    /* ignore */
  }
  const map = new Map()
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  }
}

function loadFavorites(storage) {
  try {
    const raw = storage?.getItem(FAVORITES_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : []
  } catch {
    return []
  }
}

/**
 * @param {{
 *   tracks?: Array<object>,
 *   audio?: object,
 *   storage?: object,
 *   random?: () => number,
 *   autoplayOnSelect?: boolean,
 *   volume?: number
 * }} [options]
 */
export function createPlayer(options = {}) {
  const {
    tracks = defaultTracks,
    audio = createAudioEngine(),
    storage = createSafeStorage(),
    random = Math.random,
    autoplayOnSelect = true,
  } = options

  const state = reactive({
    tracks: tracks.slice(),
    currentIndex: -1,
    isPlaying: false,
    loading: false,
    currentTime: 0,
    duration: 0,
    volume: Number.isFinite(options.volume) ? clamp01(options.volume) : 0.8,
    muted: false,
    shuffle: false,
    repeat: 'off',
    favorites: loadFavorites(storage),
    query: '',
    favoritesOnly: false,
    error: null,
  })

  audio.setVolume(state.volume)
  audio.setMuted(state.muted)

  const currentTrack = computed(() => state.tracks[state.currentIndex] ?? null)
  const visibleTracks = computed(() => {
    const filtered = filterTracks(state.tracks, state.query)
    return state.favoritesOnly ? filterFavorites(filtered, state.favorites) : filtered
  })
  const progress = computed(() =>
    state.duration > 0 ? clamp01(state.currentTime / state.duration) : 0,
  )
  const favoriteTracks = computed(() => filterFavorites(state.tracks, state.favorites))

  function persistFavorites() {
    try {
      storage?.setItem(FAVORITES_STORAGE_KEY, JSON.stringify(state.favorites))
    } catch {
      /* 存储写失败不影响播放 */
    }
  }

  /** 把某条曲目装载进引擎（不自动播放） */
  function load(index, { autoplay = false, keepTime = false } = {}) {
    const track = state.tracks[index]
    if (!track) {
      state.currentIndex = -1
      state.isPlaying = false
      state.duration = 0
      state.currentTime = 0
      return false
    }
    state.currentIndex = index
    state.error = null
    if (!keepTime) state.currentTime = 0
    if (Number.isFinite(track.duration)) state.duration = track.duration
    try {
      audio.load(track.src)
    } catch (error) {
      state.error = error?.message ?? String(error)
      state.isPlaying = false
      return false
    }
    if (autoplay) play()
    return true
  }

  function play() {
    if (!currentTrack.value) {
      // 没有任何选中曲目时，从当前可见列表（或整个歌单）第一首开始
      const first = visibleTracks.value[0] ?? state.tracks[0]
      if (!first) return false
      return load(state.tracks.indexOf(first), { autoplay: true })
    }
    state.isPlaying = true
    audio.play()
    return true
  }

  function pause() {
    state.isPlaying = false
    audio.pause()
  }

  function togglePlay() {
    if (state.isPlaying) pause()
    else play()
  }

  /** 选择曲目：点同一首正在播放的歌 = 暂停/继续 */
  function selectTrack(idOrIndex) {
    const index =
      typeof idOrIndex === 'number' ? idOrIndex : indexOfId(state.tracks, idOrIndex)
    if (index < 0 || index >= state.tracks.length) return false
    if (index === state.currentIndex) {
      togglePlay()
      return true
    }
    return load(index, { autoplay: autoplayOnSelect })
  }

  /**
   * 下一首。
   * - 手动切歌：repeat=off 时到最后一首停止；repeat=all / one 都允许越过末尾环绕。
   *   （单曲循环只作用于「自然播放结束」，手动点下一首应正常换歌）
   * - 自动切歌（播放结束）：严格按 repeat 处理，off 到末尾即停止。
   */
  function next({ auto = false } = {}) {
    const repeat = auto ? state.repeat : state.repeat === 'off' ? 'off' : 'all'
    const index = nextIndex(state.currentIndex, state.tracks.length, {
      shuffle: state.shuffle,
      repeat,
      random,
    })
    if (index < 0) {
      // 顺序播放到结尾：停下并回到 0:00
      pause()
      audio.seek(0)
      state.currentTime = 0
      return false
    }
    return load(index, { autoplay: true })
  }

  function prev() {
    const index = prevIndex(state.currentIndex, state.tracks.length, {
      shuffle: state.shuffle,
      random,
    })
    if (index < 0) return false
    return load(index, { autoplay: true })
  }

  function seek(seconds) {
    const target = Math.max(0, Math.min(state.duration || Number.POSITIVE_INFINITY, Number(seconds) || 0))
    state.currentTime = target
    audio.seek(target)
  }

  function seekRatio(ratio) {
    if (!state.duration) return
    seek(clamp01(ratio) * state.duration)
  }

  function setVolume(value) {
    const volume = clamp01(Number(value))
    state.volume = volume
    if (volume > 0 && state.muted) {
      state.muted = false
      audio.setMuted(false)
    }
    audio.setVolume(volume)
  }

  function toggleMute() {
    state.muted = !state.muted
    audio.setMuted(state.muted)
  }

  function toggleShuffle() {
    state.shuffle = !state.shuffle
  }

  function setRepeat(mode) {
    if (['off', 'all', 'one'].includes(mode)) state.repeat = mode
  }

  function cycleRepeat() {
    state.repeat = cycleRepeatMode(state.repeat)
  }

  function setQuery(query) {
    state.query = String(query ?? '')
  }

  function clearQuery() {
    state.query = ''
  }

  function toggleFavoritesOnly() {
    state.favoritesOnly = !state.favoritesOnly
  }

  function isFavorite(id) {
    return state.favorites.includes(id)
  }

  function toggleFavorite(id) {
    if (!id) return false
    const index = state.favorites.indexOf(id)
    if (index >= 0) state.favorites.splice(index, 1)
    else state.favorites.push(id)
    persistFavorites()
    return state.favorites.includes(id)
  }

  /** 播放自然结束 */
  function handleEnded() {
    if (state.repeat === 'one') {
      audio.seek(0)
      state.currentTime = 0
      audio.play()
      return true
    }
    return next({ auto: true })
  }

  function dispose() {
    audio.pause()
    audio.dispose?.()
  }

  // 订阅引擎事件，让 UI 跟随真实音频状态
  const unsubscribe = audio.on?.('timeupdate', () => {
    state.currentTime = audio.getCurrentTime()
    const realDuration = audio.getDuration?.() ?? 0
    if (realDuration > 0) state.duration = realDuration
  })
  audio.on?.('loadedmetadata', () => {
    const realDuration = audio.getDuration?.() ?? 0
    if (realDuration > 0) state.duration = realDuration
  })
  audio.on?.('play', () => {
    state.isPlaying = true
  })
  audio.on?.('pause', () => {
    state.isPlaying = false
  })
  audio.on?.('ended', () => {
    handleEnded()
  })
  audio.on?.('error', (error) => {
    state.error = error?.message ?? '音频加载失败'
    state.isPlaying = false
  })

  return reactive({
    state,
    currentTrack,
    visibleTracks,
    favoriteTracks,
    progress,
    unsubscribe,
    // actions
    play,
    pause,
    togglePlay,
    selectTrack,
    next,
    prev,
    seek,
    seekRatio,
    setVolume,
    toggleMute,
    toggleShuffle,
    setRepeat,
    cycleRepeat,
    setQuery,
    clearQuery,
    toggleFavoritesOnly,
    toggleFavorite,
    isFavorite,
    handleEnded,
    dispose,
  })
}

let singleton = null

/** 应用级单例（App.vue 使用；测试请用 createPlayer 注入假引擎） */
export function usePlayer(options) {
  if (!singleton) singleton = createPlayer(options)
  return singleton
}
