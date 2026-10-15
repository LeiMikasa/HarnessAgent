import { describe, expect, it, vi } from 'vitest'
import { createAudioEngine, resolveSrc } from '../src/utils/audio.js'

function createFakeElement() {
  const handlers = new Map()
  return {
    src: '',
    volume: 1,
    muted: false,
    preload: '',
    currentTime: 0,
    duration: 12,
    play: vi.fn(() => Promise.resolve()),
    pause: vi.fn(),
    removeAttribute(name) {
      if (name === 'src') this.src = ''
    },
    addEventListener(type, handler) {
      handlers.set(type, handler)
    },
    dispatch(type, payload) {
      handlers.get(type)?.(payload)
    },
  }
}

function setup() {
  const el = createFakeElement()
  const engine = createAudioEngine({ createElement: () => el })
  return { engine, el }
}

describe('resolveSrc', () => {
  it('虚拟音源展开为 WAV data URL', () => {
    expect(resolveSrc('tone:440:0.2').startsWith('data:audio/wav;base64,')).toBe(true)
  })

  it('普通 URL 原样返回', () => {
    expect(resolveSrc('/assets/a.mp3')).toBe('/assets/a.mp3')
    expect(resolveSrc('https://example.com/a.mp3')).toBe('https://example.com/a.mp3')
  })
})

describe('createAudioEngine', () => {
  it('load 会把音源写进元素，并对同一音源复用生成的 URL', () => {
    const { engine, el } = setup()
    const first = engine.load('tone:440:0.2')
    const second = engine.load('tone:440:0.2')
    expect(el.src).toBe(first)
    expect(second).toBe(first)
  })

  it('load 空值会清空 src', () => {
    const { engine, el } = setup()
    engine.load('/x.mp3')
    expect(el.src).toBe('/x.mp3')
    engine.load('')
    expect(el.src).toBe('')
  })

  it('缺少 createElement 时直接报错', () => {
    expect(() => createAudioEngine({ createElement: null })).toThrow()
  })

  it('play / pause 透传到元素', async () => {
    const { engine, el } = setup()
    await engine.play()
    expect(el.play).toHaveBeenCalledTimes(1)
    engine.pause()
    expect(el.pause).toHaveBeenCalledTimes(1)
  })

  it('play 被拒绝时不会抛未捕获异常，而是派发 error 事件', async () => {
    const { engine, el } = setup()
    el.play = vi.fn(() => Promise.reject(new Error('被浏览器拦截')))
    const onError = vi.fn()
    engine.on('error', onError)
    await expect(engine.play()).resolves.toBeNull()
    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('音量与静音会被夹紧后写入元素', () => {
    const { engine, el } = setup()
    engine.setVolume(1.4)
    expect(el.volume).toBe(1)
    engine.setVolume(-0.5)
    expect(el.volume).toBe(0)
    engine.setMuted(1)
    expect(el.muted).toBe(true)
  })

  it('seek 忽略非法值，负数按 0 处理', () => {
    const { engine, el } = setup()
    engine.seek(NaN)
    expect(el.currentTime).toBe(0)
    engine.seek(-4)
    expect(el.currentTime).toBe(0)
    engine.seek(3.5)
    expect(el.currentTime).toBe(3.5)
  })

  it('getDuration 对 NaN / 0 返回 0', () => {
    const { engine, el } = setup()
    el.duration = NaN
    expect(engine.getDuration()).toBe(0)
    el.duration = 0
    expect(engine.getDuration()).toBe(0)
    el.duration = 30
    expect(engine.getDuration()).toBe(30)
  })

  it('事件订阅可取消，dispose 后不再触发', () => {
    const { engine, el } = setup()
    const onEnded = vi.fn()
    const off = engine.on('ended', onEnded)
    el.dispatch('ended')
    expect(onEnded).toHaveBeenCalledTimes(1)
    off()
    el.dispatch('ended')
    expect(onEnded).toHaveBeenCalledTimes(1)
  })

  it('dispose 清空所有监听', () => {
    const { engine, el } = setup()
    const onTime = vi.fn()
    engine.on('timeupdate', onTime)
    engine.dispose()
    el.dispatch('timeupdate')
    expect(onTime).not.toHaveBeenCalled()
  })
})
