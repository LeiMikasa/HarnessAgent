import { describe, expect, it } from 'vitest'
import {
  cycleRepeatMode,
  filterFavorites,
  filterTracks,
  indexOfId,
  nextIndex,
  prevIndex,
} from '../src/utils/playlist.js'

const sample = [
  { id: 'a', title: '司空震的野区', artist: '一万二司空震', album: '峡谷回声', tags: ['电音'] },
  { id: 'b', title: '像素恋人', artist: 'Vue 三人组', album: '响应式', tags: ['City Pop'] },
  { id: 'c', title: '夜航西飞', artist: '阿翔', album: '深夜频道', tags: ['氛围'] },
]

describe('filterTracks', () => {
  it('空查询返回全部（且是新数组）', () => {
    const result = filterTracks(sample, '')
    expect(result).toHaveLength(3)
    expect(result).not.toBe(sample)
  })

  it('按标题 / 歌手 / 专辑 / 标签模糊匹配', () => {
    expect(filterTracks(sample, '夜航').map((t) => t.id)).toEqual(['c'])
    expect(filterTracks(sample, 'vue').map((t) => t.id)).toEqual(['b'])
    expect(filterTracks(sample, '响应式').map((t) => t.id)).toEqual(['b'])
    expect(filterTracks(sample, '氛围').map((t) => t.id)).toEqual(['c'])
  })

  it('忽略首尾空格与大小写', () => {
    expect(filterTracks(sample, '  CITY pop ').map((t) => t.id)).toEqual(['b'])
  })

  it('无结果时返回空数组', () => {
    expect(filterTracks(sample, '不存在的歌')).toEqual([])
  })

  it('脏数据不抛异常', () => {
    expect(filterTracks(undefined, 'x')).toEqual([])
    expect(filterTracks([null, { id: 'x', title: 'ok' }], 'ok')).toEqual([{ id: 'x', title: 'ok' }])
  })
})

describe('filterFavorites', () => {
  it('只保留收藏 id', () => {
    expect(filterFavorites(sample, ['a', 'c']).map((t) => t.id)).toEqual(['a', 'c'])
  })

  it('空收藏返回空', () => {
    expect(filterFavorites(sample, [])).toEqual([])
    expect(filterFavorites(sample, undefined)).toEqual([])
  })
})

describe('nextIndex', () => {
  it('未选曲时从第一首开始', () => {
    expect(nextIndex(-1, 3, {})).toBe(0)
  })

  it('顺序播放：最后一首且 repeat=off 返回 -1（停止）', () => {
    expect(nextIndex(1, 3, { repeat: 'off' })).toBe(2)
    expect(nextIndex(2, 3, { repeat: 'off' })).toBe(-1)
  })

  it('列表循环：最后一首回到第一首', () => {
    expect(nextIndex(2, 3, { repeat: 'all' })).toBe(0)
  })

  it('单曲循环：始终返回自己', () => {
    expect(nextIndex(1, 3, { repeat: 'one' })).toBe(1)
  })

  it('随机播放不会停在当前曲目，且可复现', () => {
    const random = () => 0
    expect(nextIndex(1, 3, { shuffle: true, random })).toBe(2)
    const random2 = () => 0.99
    expect(nextIndex(1, 3, { shuffle: true, random: random2 })).toBe(0)
  })

  it('单曲歌单在随机模式下仍是自己', () => {
    expect(nextIndex(0, 1, { shuffle: true })).toBe(0)
  })

  it('空歌单返回 -1', () => {
    expect(nextIndex(0, 0, {})).toBe(-1)
    expect(nextIndex(-1, 0, {})).toBe(-1)
  })
})

describe('prevIndex', () => {
  it('环绕到最后一首', () => {
    expect(prevIndex(0, 3)).toBe(2)
    expect(prevIndex(2, 3)).toBe(1)
  })

  it('未选曲时从第一首开始', () => {
    expect(prevIndex(-1, 3)).toBe(0)
  })

  it('空歌单返回 -1', () => {
    expect(prevIndex(0, 0)).toBe(-1)
  })

  it('随机模式下不会等于当前索引', () => {
    expect(prevIndex(1, 3, { shuffle: true, random: () => 0 })).toBe(0)
    expect(prevIndex(1, 3, { shuffle: true, random: () => 0.9 })).toBe(2)
  })
})

describe('cycleRepeatMode', () => {
  it('off -> all -> one -> off', () => {
    expect(cycleRepeatMode('off')).toBe('all')
    expect(cycleRepeatMode('all')).toBe('one')
    expect(cycleRepeatMode('one')).toBe('off')
  })

  it('未知值回到 all', () => {
    expect(cycleRepeatMode('???')).toBe('all')
  })
})

describe('indexOfId', () => {
  it('按 id 找索引', () => {
    expect(indexOfId(sample, 'c')).toBe(2)
    expect(indexOfId(sample, 'nope')).toBe(-1)
    expect(indexOfId(undefined, 'a')).toBe(-1)
  })
})
