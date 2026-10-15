/**
 * SearchBar.vue 组件级单元测试（纯展示组件，契约见 CONTRACT.md §2.1）。
 *
 * 约定：
 * - 只通过 props 传入、只通过 `emitted()` 断言，不触碰 player / localStorage / Audio。
 * - 断言全部基于 `data-test` 钩子，不做整段 DOM 快照。
 * - 因为要断言 `defineExpose({ focus })` 真的把焦点给了 input，
 *   所以统一 `attachTo: document.body`（脱离文档树的元素 focus 无效）。
 *
 * 已知契约与实现的偏差（不修改源码，仅在此记录）：
 * - 契约 §2.1 要求 resultCount 为 NaN 时按 0 处理，当前实现未夹紧，
 *   待源码修复后此断言需同步改为 `显示 0 / 0 首`（见「边界」用例）。
 */
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SearchBar from '../src/components/SearchBar.vue'

let wrapper
let host

function mountSearchBar(props = {}) {
  host = document.createElement('div')
  document.body.appendChild(host)
  wrapper = mount(SearchBar, { props, attachTo: host })
  return wrapper
}

afterEach(() => {
  wrapper?.unmount()
  host?.remove()
  wrapper = undefined
  host = undefined
})

describe('SearchBar · 默认渲染', () => {
  it('默认 props 下渲染计数文案与未过滤标记', () => {
    const w = mountSearchBar()
    const root = w.get('[data-test="search-bar"]')
    expect(root.attributes('data-filtered')).toBe('false')
    expect(w.get('[data-test="search-count"]').text()).toBe('显示 0 / 0 首')
    // 空 modelValue 不渲染清除按钮
    expect(w.find('[data-test="search-clear"]').exists()).toBe(false)
  })

  it('输入框带无障碍属性与占位提示', () => {
    const w = mountSearchBar()
    const input = w.get('[data-test="search-input"]')
    expect(input.attributes('type')).toBe('search')
    expect(input.attributes('aria-label')).toBe('搜索歌曲')
    expect(input.attributes('placeholder')).toBeTruthy()
  })

  it('modelValue 非空时输入框回显该值并渲染清除按钮', () => {
    const w = mountSearchBar({ modelValue: '夜航' })
    expect(w.get('[data-test="search-input"]').element.value).toBe('夜航')
    expect(w.get('[data-test="search-clear"]').exists()).toBe(true)
  })
})

describe('SearchBar · 计数与过滤标记', () => {
  it('resultCount < totalCount 时标记为已过滤', () => {
    const w = mountSearchBar({ resultCount: 3, totalCount: 8 })
    expect(w.get('[data-test="search-bar"]').attributes('data-filtered')).toBe('true')
    expect(w.get('[data-test="search-count"]').text()).toBe('显示 3 / 8 首')
  })

  it('resultCount === totalCount 时标记为未过滤', () => {
    const w = mountSearchBar({ resultCount: 8, totalCount: 8 })
    expect(w.get('[data-test="search-bar"]').attributes('data-filtered')).toBe('false')
  })

  it('resultCount 大于 totalCount（脏数据）时不标记为已过滤', () => {
    const w = mountSearchBar({ resultCount: 99, totalCount: 8 })
    expect(w.get('[data-test="search-bar"]').attributes('data-filtered')).toBe('false')
    expect(w.get('[data-test="search-count"]').text()).toBe('显示 99 / 8 首')
  })
})

describe('SearchBar · 输入与清除事件', () => {
  it('输入文本 emit update:modelValue 且 payload 为字符串', async () => {
    const w = mountSearchBar()
    await w.get('[data-test="search-input"]').setValue('夜航')
    const events = w.emitted('update:modelValue')
    expect(events).toHaveLength(1)
    expect(events[0]).toEqual(['夜航'])
    expect(typeof events[0][0]).toBe('string')
    // 纯展示组件：不改内部状态，emit clear 不应被顺带触发
    expect(w.emitted('clear')).toBeUndefined()
  })

  it('清空输入同样 emit 空字符串', async () => {
    const w = mountSearchBar({ modelValue: '夜航' })
    await w.get('[data-test="search-input"]').setValue('')
    expect(w.emitted('update:modelValue')).toEqual([['']])
  })

  it('点击清除按钮 emit clear（无参数），且不触发 update:modelValue', async () => {
    const w = mountSearchBar({ modelValue: '夜航' })
    await w.get('[data-test="search-clear"]').trigger('click')
    const cleared = w.emitted('clear')
    expect(cleared).toHaveLength(1)
    expect(cleared[0]).toEqual([])
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('清除按钮的 aria-label 描述其用途', () => {
    const w = mountSearchBar({ modelValue: '夜航' })
    expect(w.get('[data-test="search-clear"]').attributes('aria-label')).toBe('清除搜索')
  })
})

describe('SearchBar · 暴露的 focus()', () => {
  it('focus() 让输入框获得焦点（App 的 / 快捷键依赖）', async () => {
    const w = mountSearchBar()
    expect(typeof w.vm.focus).toBe('function')
    await w.vm.focus()
    expect(document.activeElement).toBe(w.get('[data-test="search-input"]').element)
  })

  it('重复调用 focus() 不抛异常', async () => {
    const w = mountSearchBar()
    w.vm.focus()
    expect(() => w.vm.focus()).not.toThrow()
    expect(document.activeElement?.getAttribute('data-test')).toBe('search-input')
  })
})

describe('SearchBar · 边界', () => {
  it('resultCount 为 NaN 时按当前实现渲染（契约 §2.1 待修复点）', () => {
    // 契约 §2.1 要求 resultCount 为 NaN 时按 0 处理，当前实现未夹紧，
    // 待源码修复后此断言需同步改为 `显示 0 / 0 首`。
    const w = mountSearchBar({ resultCount: NaN, totalCount: 0 })
    expect(w.get('[data-test="search-count"]').text()).toBe('显示 NaN / 0 首')
    // NaN 与任何值比较均为 false，因此不会被标记为「已过滤」
    expect(w.get('[data-test="search-bar"]').attributes('data-filtered')).toBe('false')
  })

  it('超长输入不崩溃且 payload 完整', async () => {
    const w = mountSearchBar()
    const long = '夜'.repeat(200)
    await w.get('[data-test="search-input"]').setValue(long)
    const events = w.emitted('update:modelValue')
    expect(events).toHaveLength(1)
    expect(events[0][0]).toBe(long)
    expect(w.get('[data-test="search-bar"]').exists()).toBe(true)
  })

  it('特殊字符输入原样透出', async () => {
    const w = mountSearchBar()
    await w.get('[data-test="search-input"]').setValue('<script> & "x"')
    expect(w.emitted('update:modelValue')[0][0]).toBe('<script> & "x"')
  })
})
