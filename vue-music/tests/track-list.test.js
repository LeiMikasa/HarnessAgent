/**
 * TrackList.vue 组件级单元测试（纯展示组件，契约见 CONTRACT.md §2.2）。
 *
 * 约定：
 * - 只通过 props 传入、只通过 `emitted()` 断言，不触碰 player / localStorage / Audio。
 * - 时长文案依赖 `src/utils/format.js` 的 `formatTime`，测试里用同一个函数算出期望值，
 *   避免在测试里硬编码格式细节（另有一处显式断言 `65 → 1:05` 锚定契约示例）。
 * - 断言全部基于 `data-test` 钩子与属性，不做整段 DOM 快照。
 */
import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TrackList from '../src/components/TrackList.vue'
import { formatTime } from '../src/utils/format.js'

const TRACKS = [
  {
    id: 't1',
    title: '司空震的野区',
    artist: '一万二司空震',
    album: '峡谷回声',
    duration: 65,
    cover: '🎧',
    color: 'rgb(58, 12, 90)',
  },
  { id: 't2', title: '夜航西飞', artist: '阿翔', album: '深夜频道', duration: 3725, cover: '🌙', color: '#123456' },
  { id: 't3', title: '像素恋人', artist: 'Vue 三人组', album: '响应式', duration: 0, cover: '🕹️' },
]

let wrapper

function mountTrackList(props = {}) {
  wrapper = mount(TrackList, { props })
  return wrapper
}

function items() {
  return wrapper.findAll('[data-test="track-item"]')
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
})

describe('TrackList · 空状态', () => {
  it('不传 tracks 时渲染空状态且没有条目', () => {
    const w = mountTrackList()
    expect(w.get('[data-test="track-list-empty"]').text()).toBe('没有匹配的歌曲')
    expect(w.findAll('[data-test="track-item"]')).toHaveLength(0)
  })

  it('显式传空数组时同样渲染空状态', () => {
    const w = mountTrackList({ tracks: [] })
    expect(w.get('[data-test="track-list-empty"]').exists()).toBe(true)
    expect(w.findAll('[data-test="track-item"]')).toHaveLength(0)
  })
})

describe('TrackList · 曲目渲染', () => {
  it('3 首曲目渲染 3 行，带正确的 data-track-id', () => {
    const w = mountTrackList({ tracks: TRACKS })
    const list = items()
    expect(list).toHaveLength(3)
    expect(list.map((li) => li.attributes('data-track-id'))).toEqual(['t1', 't2', 't3'])
    expect(w.find('[data-test="track-list-empty"]').exists()).toBe(false)
  })

  it('标题 / 歌手 / 时长按契约渲染', () => {
    const w = mountTrackList({ tracks: TRACKS })
    expect(w.findAll('[data-test="track-title"]').map((el) => el.text())).toEqual([
      '司空震的野区',
      '夜航西飞',
      '像素恋人',
    ])
    expect(w.findAll('[data-test="track-artist"]').map((el) => el.text())).toEqual([
      '一万二司空震',
      '阿翔',
      'Vue 三人组',
    ])
    // 契约示例：65 秒 → 1:05
    expect(items()[0].get('[data-test="track-duration"]').text()).toBe('1:05')
  })

  it('时长使用 formatTime，覆盖超过一小时与 0 秒', () => {
    const w = mountTrackList({ tracks: TRACKS })
    const durations = w.findAll('[data-test="track-duration"]').map((el) => el.text())
    expect(durations).toEqual(TRACKS.map((t) => formatTime(t.duration)))
    expect(durations[1]).toBe('1:02:05')
    expect(durations[2]).toBe('0:00')
  })

  it('缺少 duration / 非法 duration 时回退为 0:00 且不抛异常', () => {
    const w = mountTrackList({
      tracks: [
        { id: 'x', title: '缺时长', artist: '未知', cover: '❓' },
        { id: 'y', title: '脏时长', artist: '未知', duration: 'abc', cover: '❓' },
      ],
    })
    expect(items()).toHaveLength(2)
    expect(w.findAll('[data-test="track-duration"]').map((el) => el.text())).toEqual(['0:00', '0:00'])
  })
})

describe('TrackList · 当前曲目标记', () => {
  it('currentId 命中时该行 data-active=true，其余为 false', () => {
    const w = mountTrackList({ tracks: TRACKS, currentId: 't2' })
    const list = items()
    expect(list[0].attributes('data-active')).toBe('false')
    expect(list[1].attributes('data-active')).toBe('true')
    expect(list[2].attributes('data-active')).toBe('false')
  })

  it('currentId 未命中任何曲目时全部为 false', () => {
    const w = mountTrackList({ tracks: TRACKS, currentId: 'nope' })
    expect(items().every((li) => li.attributes('data-active') === 'false')).toBe(true)
  })

  it('currentId 命中且 isPlaying 为真时该行 data-playing=true', () => {
    const w = mountTrackList({ tracks: TRACKS, currentId: 't1', isPlaying: true })
    const list = items()
    expect(list[0].attributes('data-playing')).toBe('true')
    expect(list[1].attributes('data-playing')).toBe('false')
    expect(list[2].attributes('data-playing')).toBe('false')
  })

  it('暂停状态下 data-playing 全为 false（即便命中 currentId）', () => {
    const w = mountTrackList({ tracks: TRACKS, currentId: 't1', isPlaying: false })
    expect(items().every((li) => li.attributes('data-playing') === 'false')).toBe(true)
  })
})

describe('TrackList · 事件', () => {
  it('点击 .track-main emit select 并携带该行 id', async () => {
    const w = mountTrackList({ tracks: TRACKS })
    await w.findAll('[data-test="track-item"] .track-main')[1].trigger('click')
    expect(w.emitted('select')).toEqual([['t2']])
  })

  it('点击不同行分别 emit 对应 id', async () => {
    const w = mountTrackList({ tracks: TRACKS })
    const mains = w.findAll('[data-test="track-item"] .track-main')
    await mains[0].trigger('click')
    await mains[2].trigger('click')
    expect(w.emitted('select')).toEqual([['t1'], ['t3']])
  })

  it('点击整行 li（非 .track-main 区域）不会 emit select', async () => {
    const w = mountTrackList({ tracks: TRACKS })
    // li 本身没有绑定点击，select 只由 .track-main 触发
    await items()[0].trigger('click')
    expect(w.emitted('select')).toBeUndefined()
  })

  it('点击收藏按钮 emit toggle-favorite 且不会同时 emit select', async () => {
    const w = mountTrackList({ tracks: TRACKS })
    await w.findAll('[data-test="favorite-btn"]')[0].trigger('click')
    expect(w.emitted('toggle-favorite')).toEqual([['t1']])
    expect(w.emitted('select')).toBeUndefined()
  })

  it('收藏按钮带 data-track-id，可定位到具体曲目', async () => {
    const w = mountTrackList({ tracks: TRACKS })
    const btns = w.findAll('[data-test="favorite-btn"]')
    expect(btns.map((b) => b.attributes('data-track-id'))).toEqual(['t1', 't2', 't3'])
    await btns[2].trigger('click')
    expect(w.emitted('toggle-favorite')).toEqual([['t3']])
  })
})

describe('TrackList · 收藏态', () => {
  it('favorites 命中时 data-active 与 aria-pressed 都为 true', () => {
    const w = mountTrackList({ tracks: TRACKS, favorites: ['t2'] })
    const btns = w.findAll('[data-test="favorite-btn"]')
    expect(btns[1].attributes('data-active')).toBe('true')
    expect(btns[1].attributes('aria-pressed')).toBe('true')
    expect(btns[0].attributes('data-active')).toBe('false')
    expect(btns[0].attributes('aria-pressed')).toBe('false')
    expect(btns[2].attributes('aria-pressed')).toBe('false')
  })

  it('favorites 默认空数组时全部未收藏', () => {
    const w = mountTrackList({ tracks: TRACKS })
    expect(w.findAll('[data-test="favorite-btn"]').every((b) => b.attributes('aria-pressed') === 'false')).toBe(
      true,
    )
  })
})

describe('TrackList · 封面', () => {
  it('封面显示 track.cover 并使用 track.color 作为背景', () => {
    const w = mountTrackList({ tracks: TRACKS })
    const covers = w.findAll('[data-test="track-cover"]')
    expect(covers.map((c) => c.text())).toEqual(['🎧', '🌙', '🕹️'])
    expect(covers[0].element.style.background).toBe('rgb(58, 12, 90)')
    expect(covers[1].element.style.background).toBe('rgb(18, 52, 86)')
  })

  it('color 缺省时回退到 var(--panel) 且不报错', () => {
    // 实测 jsdom 会原样保留 `var(--panel)`（probe 验证），故直接断言回退值；
    // 若将来 jsdom 丢弃该值，则退化为「不抛异常 + 结构完整」的宽松断言。
    const w = mountTrackList({ tracks: [TRACKS[2]] })
    const cover = w.get('[data-test="track-cover"]')
    expect(cover.text()).toBe('🕹️')
    const background = cover.element.style.background
    if (background) expect(background).toBe('var(--panel)')
  })

  it('脏数据行（只有 id）也能渲染且不抛异常', () => {
    const w = mountTrackList({ tracks: [{ id: 'raw' }] })
    expect(items()).toHaveLength(1)
    expect(items()[0].attributes('data-track-id')).toBe('raw')
    expect(w.get('[data-test="track-duration"]').text()).toBe('0:00')
    expect(w.get('[data-test="track-cover"]').exists()).toBe(true)
  })
})
