/**
 * 内存版音频引擎，接口与 src/utils/audio.js 的 createAudioEngine 完全一致。
 * 组件测试 / 播放器测试用它替代真实 <audio>，因此测试环境不需要能出声。
 */
export function createFakeAudio(options = {}) {
  const listeners = new Map()
  const calls = { load: [], play: 0, pause: 0, seek: [], setVolume: [], setMuted: [] }
  const el = {
    src: '',
    currentTime: 0,
    duration: Number.isFinite(options.duration) ? options.duration : 0,
    volume: 1,
    muted: false,
  }
  let disposed = false

  const emit = (type, payload) => {
    for (const handler of listeners.get(type) ?? []) handler(payload)
  }

  return {
    el,
    calls,

    load(src) {
      calls.load.push(src)
      el.src = src
      el.currentTime = 0
      return src
    },
    play() {
      calls.play += 1
      emit('play')
      return Promise.resolve()
    },
    pause() {
      calls.pause += 1
      emit('pause')
    },
    setVolume(volume) {
      calls.setVolume.push(volume)
      el.volume = volume
    },
    setMuted(muted) {
      calls.setMuted.push(muted)
      el.muted = muted
    },
    seek(seconds) {
      calls.seek.push(seconds)
      el.currentTime = seconds
    },
    getCurrentTime() {
      return el.currentTime
    },
    getDuration() {
      return el.duration
    },
    on(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(handler)
      return () => listeners.get(type)?.delete(handler)
    },
    dispose() {
      disposed = true
      listeners.clear()
    },
    get disposed() {
      return disposed
    },

    // ---- 测试辅助 ----
    /** 模拟播放进度推进 */
    advance(seconds) {
      el.currentTime += seconds
      emit('timeupdate')
    },
    /** 模拟元数据就绪（可覆盖时长） */
    ready(duration) {
      if (Number.isFinite(duration)) el.duration = duration
      emit('loadedmetadata')
    },
    /** 模拟播放自然结束 */
    end() {
      emit('ended')
    },
    /** 模拟加载失败 */
    fail(message = '加载失败') {
      emit('error', new Error(message))
    },
  }
}

export default createFakeAudio
