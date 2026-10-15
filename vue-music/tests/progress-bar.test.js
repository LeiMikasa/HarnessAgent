/**
 * ProgressBar.vue 组件级单元测试（纯展示组件，CONTRACT.md §2.3）。
 *
 * 实现偏差备注：源码用 `isDisabled = disabled || duration <= 0` 判断禁用，
 * 而 `NaN <= 0` 为 false，因此 duration=NaN 时进度条**不会**置灰
 * （按契约意图，非法时长应视为禁用）。下面 duration=NaN 的用例只断言当前
 * 实现的真实行为；日后若修复源码（例如改为 `!(duration > 0)`），需同步更新该用例。
 */
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ProgressBar from '../src/components/ProgressBar.vue'

let wrapper

afterEach(() => {
  if (wrapper) wrapper.unmount()
  wrapper = null
})

function mountProgress(props = {}) {
  wrapper = mount(ProgressBar, { props })
  return wrapper
}

describe('ProgressBar 缺省与正常渲染', () => {
  it('缺省 props：duration=0 → data-disabled=true、range 禁用、两端文案 0:00', () => {
    mountProgress()
    expect(wrapper.get('[data-test="progress-bar"]').attributes('data-disabled')).toBe('true')
    expect(wrapper.get('[data-test="progress-range"]').element.disabled).toBe(true)
    expect(wrapper.get('[data-test="progress-current"]').text()).toBe('0:00')
    expect(wrapper.get('[data-test="progress-duration"]').text()).toBe('0:00')
    expect(wrapper.get('[data-test="progress-fill"]').attributes('style')).toContain('0%')
  })

  it('currentTime=12 / duration=24 → 填充 50%、range value=50、文案 0:12 / 0:24', () => {
    mountProgress({ currentTime: 12, duration: 24 })
    expect(wrapper.get('[data-test="progress-bar"]').attributes('data-disabled')).toBe('false')
    expect(wrapper.get('[data-test="progress-range"]').element.disabled).toBe(false)
    expect(wrapper.get('[data-test="progress-fill"]').attributes('style')).toContain('50%')
    expect(wrapper.get('[data-test="progress-range"]').element.value).toBe('50')
    expect(wrapper.get('[data-test="progress-current"]').text()).toBe('0:12')
    expect(wrapper.get('[data-test="progress-duration"]').text()).toBe('0:24')
  })
})

describe('ProgressBar 交互', () => {
  it('拖动 range 到 50 → emit seek，payload 为 duration 的一半', async () => {
    mountProgress({ currentTime: 0, duration: 24 })
    await wrapper.get('[data-test="progress-range"]').setValue('50')
    const events = wrapper.emitted('seek')
    expect(events).toHaveLength(1)
    expect(events[0][0]).toBeCloseTo(12, 5)
    expect(events[0][0]).toBe(12)
  })

  it('seek 目标秒数按 duration 缩放（30% × 120s = 36s）', async () => {
    mountProgress({ currentTime: 0, duration: 120 })
    await wrapper.get('[data-test="progress-range"]').setValue('30')
    const events = wrapper.emitted('seek')
    expect(events).toHaveLength(1)
    expect(events[0][0]).toBeCloseTo(36, 5)
  })
})

describe('ProgressBar 边界', () => {
  it('currentTime > duration：进度夹紧到 100%', () => {
    mountProgress({ currentTime: 30, duration: 24 })
    expect(wrapper.get('[data-test="progress-fill"]').attributes('style')).toContain('100%')
    expect(wrapper.get('[data-test="progress-range"]').element.value).toBe('100')
    expect(wrapper.get('[data-test="progress-duration"]').text()).toBe('0:24')
  })

  it('currentTime < 0：进度为 0% 且文案回退 0:00', () => {
    mountProgress({ currentTime: -5, duration: 24 })
    expect(wrapper.get('[data-test="progress-fill"]').attributes('style')).toContain('0%')
    expect(wrapper.get('[data-test="progress-current"]').text()).toBe('0:00')
  })

  it('duration=NaN：不崩溃、进度 0%、时长文案 0:00（但 data-disabled 仍为 false，见文件头备注）', () => {
    mountProgress({ currentTime: 10, duration: NaN })
    expect(wrapper.get('[data-test="progress-bar"]').attributes('data-disabled')).toBe('false')
    expect(wrapper.get('[data-test="progress-fill"]').attributes('style')).toContain('0%')
    expect(wrapper.get('[data-test="progress-range"]').element.value).toBe('0')
    expect(wrapper.get('[data-test="progress-duration"]').text()).toBe('0:00')
  })

  it('显式 disabled=true：即使 duration 有效也置灰并禁用 range', () => {
    mountProgress({ currentTime: 12, duration: 24, disabled: true })
    expect(wrapper.get('[data-test="progress-bar"]').attributes('data-disabled')).toBe('true')
    expect(wrapper.get('[data-test="progress-range"]').element.disabled).toBe(true)
  })

  it('缺失 currentTime 时按默认 0 处理', () => {
    mountProgress({ duration: 24 })
    expect(wrapper.get('[data-test="progress-current"]').text()).toBe('0:00')
    expect(wrapper.get('[data-test="progress-fill"]').attributes('style')).toContain('0%')
  })
})
