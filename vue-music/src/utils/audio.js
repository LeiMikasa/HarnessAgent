/**
 * 音频引擎：对 HTMLAudioElement 的一层薄封装。
 *
 * 播放器逻辑只依赖这里暴露的接口，因此单元测试可以注入一个内存版假引擎
 * （见 tests/helpers/fake-audio.js），完全不需要真实的 <audio> 元素。
 */

import { makeToneDataUrl, parseToneSrc } from './tone.js'

/**
 * 解析真实可播放的 URL：`tone:*` 走程序化生成，其余原样返回。
 * @param {string} src
 * @returns {string}
 */
export function resolveSrc(src) {
  if (parseToneSrc(src)) return makeToneDataUrl(src)
  return src
}

/**
 * @param {{createElement?: (tag: string) => any}} [options]
 * @returns {object} 音频引擎
 */
export function createAudioEngine(options = {}) {
  // 显式传入 createElement: null 视为「缺少实现」，直接报错；未传时退回 document。
  const explicit = 'createElement' in options ? options.createElement : undefined
  const fallback =
    typeof document !== 'undefined' && typeof document.createElement === 'function'
      ? document.createElement.bind(document)
      : null
  const createElement =
    typeof explicit === 'function' ? explicit : explicit === undefined ? fallback : explicit
  if (typeof createElement !== 'function') {
    throw new Error('createAudioEngine 需要一个 createElement 实现')
  }

  const el = createElement('audio')
  el.preload = 'metadata'
  const listeners = new Map()
  const urlCache = new Map()

  const emit = (type, payload) => {
    const handlers = listeners.get(type)
    if (!handlers) return
    for (const handler of [...handlers]) handler(payload)
  }

  const bind = (type) => el.addEventListener(type, (event) => emit(type, event))

  bind('timeupdate')
  bind('ended')
  bind('loadedmetadata')
  bind('play')
  bind('pause')
  bind('error')

  return {
    el,

    /** 加载音源（`tone:*` 会被展开成 WAV data URL，并做缓存） */
    load(src) {
      if (!src) {
        el.removeAttribute('src')
        return null
      }
      let resolved = urlCache.get(src)
      if (!resolved) {
        resolved = resolveSrc(src)
        urlCache.set(src, resolved)
      }
      el.src = resolved
      return resolved
    },

    play() {
      const result = el.play()
      // 浏览器的 play() 返回 Promise，播放被中断时会 reject，这里吞掉避免 unhandled rejection
      if (result && typeof result.catch === 'function') {
        return result.catch((error) => {
          emit('error', error)
          return null
        })
      }
      return Promise.resolve(result ?? null)
    },

    pause() {
      if (typeof el.pause === 'function') el.pause()
    },

    setVolume(volume) {
      el.volume = Math.min(1, Math.max(0, volume))
    },

    setMuted(muted) {
      el.muted = Boolean(muted)
    },

    seek(seconds) {
      if (Number.isFinite(seconds)) {
        try {
          el.currentTime = Math.max(0, seconds)
        } catch {
          /* 部分环境在 metadata 之前禁止设置 currentTime，忽略即可 */
        }
      }
    },

    getCurrentTime() {
      return Number.isFinite(el.currentTime) ? el.currentTime : 0
    },

    getDuration() {
      return Number.isFinite(el.duration) && el.duration > 0 ? el.duration : 0
    },

    /** 监听引擎事件：timeupdate / ended / loadedmetadata / play / pause / error */
    on(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(handler)
      return () => listeners.get(type)?.delete(handler)
    },

    /** 解绑全部监听（组件卸载时调用） */
    dispose() {
      listeners.clear()
      urlCache.clear()
    },
  }
}
