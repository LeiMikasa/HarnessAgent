/**
 * VolumeControl.vue 组件级单元测试（纯展示组件，CONTRACT.md §2.4）。
 *
 * 实现观察：越界/NaN 的 volume 直接来自 `Math.round(volume * 100)` 绑定到 range，
 * 未经 formatVolume 夹紧；不过 `<input type="range">` 的原生“值消毒算法”会把 DOM 上的
 * `value` 夹紧到 [min,max]（NaN 则回退到区间中点）。下面用例只断言真实可观测行为。
 */
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import VolumeControl from '../src/components/VolumeControl.vue'

let wrapper

afterEach(() => {
  if (wrapper) wrapper.unmount()
  wrapper = null
})

function mountVolume(props = {}) {
  wrapper = mount(VolumeControl, { props })
  return wrapper
}

describe('VolumeControl 正常渲染', () => {
  it('默认 volume=1 / muted=false → range value=100、喇叭 🔊、文案 100%', () => {
    mountVolume()
    expect(wrapper.get('[data-test="volume-control"]').attributes('data-muted')).toBe('false')
    expect(wrapper.get('[data-test="volume-range"]').element.value).toBe('100')
    expect(wrapper.get('[data-test="volume-mute"]').text()).toBe('🔊')
    expect(wrapper.get('[data-test="volume-mute"]').attributes('aria-pressed')).toBe('false')
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('100%')
  })

  it('volume=0.35 → range value=35、文案与 formatVolume 一致（35%）', () => {
    mountVolume({ volume: 0.35 })
    expect(wrapper.get('[data-test="volume-range"]').element.value).toBe('35')
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('35%')
  })

  it('muted=true → 图标 🔇、data-muted=true、aria-pressed=true、文案 静音', () => {
    mountVolume({ volume: 0.6, muted: true })
    expect(wrapper.get('[data-test="volume-control"]').attributes('data-muted')).toBe('true')
    const mute = wrapper.get('[data-test="volume-mute"]')
    expect(mute.text()).toBe('🔇')
    expect(mute.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('静音')
  })
})

describe('VolumeControl 交互', () => {
  it('拖动 range 到 40 → emit update:volume，payload 为 0.4', async () => {
    mountVolume()
    await wrapper.get('[data-test="volume-range"]').setValue('40')
    const events = wrapper.emitted('update:volume')
    expect(events).toHaveLength(1)
    expect(events[0][0]).toBeCloseTo(0.4, 5)
  })

  it('拖动 range 到 0 / 100 → payload 为 0 / 1', async () => {
    mountVolume({ volume: 0.5 })
    await wrapper.get('[data-test="volume-range"]').setValue('0')
    await wrapper.get('[data-test="volume-range"]').setValue('100')
    const events = wrapper.emitted('update:volume')
    expect(events).toHaveLength(2)
    expect(events[0][0]).toBeCloseTo(0, 5)
    expect(events[1][0]).toBeCloseTo(1, 5)
  })

  it('点击喇叭按钮 → emit toggle-mute（无参数）', async () => {
    mountVolume()
    await wrapper.get('[data-test="volume-mute"]').trigger('click')
    const events = wrapper.emitted('toggle-mute')
    expect(events).toHaveLength(1)
    expect(events[0]).toEqual([])
  })
})

describe('VolumeControl 边界', () => {
  it('volume=1.4 越界：不崩溃，文案夹紧为 100%，range DOM 值被原生消毒夹紧到 max', () => {
    mountVolume({ volume: 1.4 })
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('100%')
    // 真实行为：组件绑定 :value=Math.round(1.4*100)=140，但 <input type=range>
    // 的原生“值消毒算法”会把 DOM 属性值夹紧到 max=100（jsdom 同样实现），故读数为 100。
    expect(wrapper.get('[data-test="volume-range"]').element.value).toBe('100')
  })

  it('volume=-0.5 越界：不崩溃，文案夹紧为 0%，range DOM 值被原生消毒夹紧到 min', () => {
    mountVolume({ volume: -0.5 })
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('0%')
    // 真实行为：绑定 :value=Math.round(-0.5*100)=-50，原生值消毒把 DOM 值夹紧到 min=0。
    expect(wrapper.get('[data-test="volume-range"]').element.value).toBe('0')
  })

  it('volume=NaN：不崩溃，文案为 0%', () => {
    mountVolume({ volume: NaN })
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('0%')
    // 绑定值为 Math.round(NaN*100)=NaN，range 值消毒回退到区间中点 50。
    expect(wrapper.get('[data-test="volume-range"]').element.value).toBe('50')
  })
})
