/**
 * PlayerBar.vue 组件级单元测试（纯展示组件，CONTRACT.md §2.5）。
 *
 * 该组件模板内联使用了真实的 <ProgressBar> 与 <VolumeControl> 子组件，
 * 这里除了验证自身按钮/文案，还验证子组件事件透传：
 *   ProgressBar @seek          → 外层 emit('seek')
 *   VolumeControl @update:volume → 外层 emit('volume')
 *   VolumeControl @toggle-mute   → 外层 emit('toggle-mute')
 */
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PlayerBar from '../src/components/PlayerBar.vue'

const TRACK = { id: 't1', title: '夜航西飞', artist: '李知恩' }

let wrapper

afterEach(() => {
  if (wrapper) wrapper.unmount()
  wrapper = null
})

function mountBar(props = {}) {
  wrapper = mount(PlayerBar, { props })
  return wrapper
}

describe('PlayerBar 当前曲目信息', () => {
  it('无曲目：标题 未选择歌曲、歌手 —', () => {
    mountBar()
    expect(wrapper.get('[data-test="now-playing-title"]').text()).toBe('未选择歌曲')
    expect(wrapper.get('[data-test="now-playing-artist"]').text()).toBe('—')
  })

  it('传入曲目：显示 title / artist', () => {
    mountBar({ track: TRACK })
    expect(wrapper.get('[data-test="now-playing-title"]').text()).toBe('夜航西飞')
    expect(wrapper.get('[data-test="now-playing-artist"]').text()).toBe('李知恩')
  })
})

describe('PlayerBar 播放/暂停按钮', () => {
  it('isPlaying=false → aria-label 播放、图标 ▶', () => {
    mountBar({ isPlaying: false })
    const btn = wrapper.get('[data-test="play-toggle"]')
    expect(btn.attributes('aria-label')).toBe('播放')
    expect(btn.text()).toBe('▶')
  })

  it('isPlaying=true → aria-label 暂停、图标 ❚❚', () => {
    mountBar({ isPlaying: true })
    const btn = wrapper.get('[data-test="play-toggle"]')
    expect(btn.attributes('aria-label')).toBe('暂停')
    expect(btn.text()).toBe('❚❚')
  })
})

describe('PlayerBar repeat 模式', () => {
  it('off/all/one → data-repeat 与 aria-label 一一对应', () => {
    const cases = [
      ['off', '顺序播放'],
      ['all', '列表循环'],
      ['one', '单曲循环'],
    ]
    for (const [mode, label] of cases) {
      mountBar({ repeat: mode })
      const btn = wrapper.get('[data-test="repeat"]')
      expect(btn.attributes('data-repeat')).toBe(mode)
      expect(btn.attributes('aria-label')).toBe(label)
      wrapper.unmount()
      wrapper = null
    }
  })

  it('repeat 为未知值时回退到 off 的文案', () => {
    mountBar({ repeat: 'bogus' })
    expect(wrapper.get('[data-test="repeat"]').attributes('data-repeat')).toBe('bogus')
    expect(wrapper.get('[data-test="repeat"]').attributes('aria-label')).toBe('顺序播放')
  })
})

describe('PlayerBar shuffle 状态', () => {
  it('shuffle=true → aria-pressed=true', () => {
    mountBar({ shuffle: true })
    expect(wrapper.get('[data-test="shuffle"]').attributes('aria-pressed')).toBe('true')
  })

  it('shuffle=false → aria-pressed=false', () => {
    mountBar({ shuffle: false })
    expect(wrapper.get('[data-test="shuffle"]').attributes('aria-pressed')).toBe('false')
  })
})

describe('PlayerBar 自身按钮事件', () => {
  it('点击 shuffle/prev/next/play-toggle/repeat 分别 emit 对应事件', async () => {
    mountBar()
    await wrapper.get('[data-test="shuffle"]').trigger('click')
    await wrapper.get('[data-test="prev"]').trigger('click')
    await wrapper.get('[data-test="next"]').trigger('click')
    await wrapper.get('[data-test="play-toggle"]').trigger('click')
    await wrapper.get('[data-test="repeat"]').trigger('click')

    expect(wrapper.emitted('toggle-shuffle')).toHaveLength(1)
    expect(wrapper.emitted('prev')).toHaveLength(1)
    expect(wrapper.emitted('next')).toHaveLength(1)
    expect(wrapper.emitted('toggle-play')).toHaveLength(1)
    expect(wrapper.emitted('cycle-repeat')).toHaveLength(1)
  })
})

describe('PlayerBar 子组件事件透传', () => {
  it('内层 ProgressBar 的 seek → 外层 emit seek（payload 秒数）', async () => {
    mountBar({ currentTime: 0, duration: 24 })
    const inner = wrapper.get('[data-test="progress-range"]')
    expect(inner.element.disabled).toBe(false)
    await inner.setValue('50')
    const events = wrapper.emitted('seek')
    expect(events).toHaveLength(1)
    expect(events[0][0]).toBeCloseTo(12, 5)
  })

  it('内层 VolumeControl 的 update:volume → 外层 emit volume（payload 0~1）', async () => {
    mountBar({ volume: 0.5 })
    await wrapper.get('[data-test="volume-range"]').setValue('40')
    const events = wrapper.emitted('volume')
    expect(events).toHaveLength(1)
    expect(events[0][0]).toBeCloseTo(0.4, 5)
    // 不应把子组件事件名原样冒出
    expect(wrapper.emitted('update:volume')).toBeUndefined()
  })

  it('内层 VolumeControl 的 toggle-mute → 外层 emit toggle-mute', async () => {
    mountBar({ volume: 0.5, muted: false })
    await wrapper.get('[data-test="volume-mute"]').trigger('click')
    expect(wrapper.emitted('toggle-mute')).toHaveLength(1)
  })

  it('子组件按传入 props 渲染（时长/音量/静音态贯通）', () => {
    mountBar({ currentTime: 6, duration: 24, volume: 0.35, muted: true })
    expect(wrapper.get('[data-test="progress-current"]').text()).toBe('0:06')
    expect(wrapper.get('[data-test="progress-duration"]').text()).toBe('0:24')
    expect(wrapper.get('[data-test="volume-range"]').element.value).toBe('35')
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('静音')
    expect(wrapper.get('[data-test="volume-control"]').attributes('data-muted')).toBe('true')
  })
})
