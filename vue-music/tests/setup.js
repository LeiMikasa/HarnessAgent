/**
 * vitest 全局配置：jsdom 环境下补齐 HTMLMediaElement 的行为，
 * 并让每个用例从干净的 localStorage 开始。
 */
import { beforeEach, vi } from 'vitest'

if (typeof window !== 'undefined' && window.HTMLMediaElement) {
  // jsdom 的 play()/pause() 默认会抛 "Not implemented"
  window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined)
  window.HTMLMediaElement.prototype.pause = vi.fn()
  window.HTMLMediaElement.prototype.load = vi.fn()
}

beforeEach(() => {
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})
