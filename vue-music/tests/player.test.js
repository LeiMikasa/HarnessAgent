import { beforeEach, describe, expect, it } from 'vitest'
import { createPlayer, createSafeStorage } from '../src/composables/usePlayer.js'
import { createFakeAudio } from './helpers/fake-audio.js'

const tracks = [
  { id: 'a', title: '第一首', artist: 'A', src: 'tone:392:6', duration: 6 },
  { id: 'b', title: '第二首', artist: 'B', src: 'tone:440:8', duration: 8 },
  { id: 'c', title: '第三首', artist: 'C', src: 'tone:523:10', duration: 10 },
]

function setup(options = {}) {
  const audio = createFakeAudio()
  const storage = createSafeStorage()
  const player = createPlayer({ tracks, audio, storage, ...options })
  return { player, audio, storage }
}

describe('createPlayer 初始化', () => {
  it('默认没有选中曲目', () => {
    const { player, audio } = setup()
    expect(player.state.currentIndex).toBe(-1)
    expect(player.currentTrack).toBeNull()
    expect(player.state.isPlaying).toBe(false)
    expect(player.state.currentTime).toBe(0)
    expect(player.state.repeat).toBe('off')
    expect(player.state.shuffle).toBe(false)
    expect(player.visibleTracks).toHaveLength(3)
    expect(audio.calls.setVolume).toEqual([0.8])
  })

  it('音量参数越界会被夹紧', () => {
    expect(setup({ volume: 3 }).player.state.volume).toBe(1)
    expect(setup({ volume: -1 }).player.state.volume).toBe(0)
  })
})

describe('播放控制', () => {
  it('未选曲时 play() 自动加载第一首并播放', () => {
    const { player, audio } = setup()
    expect(player.play()).toBe(true)
    expect(audio.calls.load).toEqual(['tone:392:6'])
    expect(player.state.currentIndex).toBe(0)
    expect(player.state.isPlaying).toBe(true)
    expect(audio.calls.play).toBe(1)
  })

  it('togglePlay 在播放/暂停之间切换', () => {
    const { player, audio } = setup()
    player.togglePlay()
    expect(player.state.isPlaying).toBe(true)
    player.togglePlay()
    expect(player.state.isPlaying).toBe(false)
    expect(audio.calls.pause).toBe(1)
  })

  it('selectTrack 按 id 切歌并重置进度', () => {
    const { player, audio } = setup()
    player.selectTrack('a')
    audio.advance(3)
    expect(player.state.currentTime).toBe(3)
    player.selectTrack('c')
    expect(player.currentTrack.id).toBe('c')
    expect(player.state.currentTime).toBe(0)
    expect(player.state.duration).toBe(10)
    expect(audio.calls.load.at(-1)).toBe('tone:523:10')
  })

  it('点击正在播放的同一首 = 暂停', () => {
    const { player } = setup()
    player.selectTrack('b')
    expect(player.state.isPlaying).toBe(true)
    player.selectTrack('b')
    expect(player.state.isPlaying).toBe(false)
  })

  it('selectTrack 遇到不存在的 id 返回 false 且不改变状态', () => {
    const { player } = setup()
    expect(player.selectTrack('zzz')).toBe(false)
    expect(player.state.currentIndex).toBe(-1)
  })

  it('autoplayOnSelect=false 时切歌不自动播放', () => {
    const { player } = setup({ autoplayOnSelect: false })
    player.selectTrack('a')
    expect(player.state.currentIndex).toBe(0)
    expect(player.state.isPlaying).toBe(false)
  })
})

describe('切歌与循环模式', () => {
  it('next 依次前进，最后一首在 repeat=off 时停止播放', () => {
    const { player, audio } = setup()
    player.selectTrack('a')
    player.next()
    expect(player.currentTrack.id).toBe('b')
    player.next()
    expect(player.currentTrack.id).toBe('c')
    const before = audio.calls.play
    expect(player.next()).toBe(false)
    expect(player.state.isPlaying).toBe(false)
    expect(audio.calls.seek.at(-1)).toBe(0)
    expect(player.currentTrack.id).toBe('c')
    expect(audio.calls.play).toBe(before)
  })

  it('repeat=all 时最后一首 next 回到第一首', () => {
    const { player } = setup()
    player.setRepeat('all')
    player.selectTrack('c')
    player.next()
    expect(player.currentTrack.id).toBe('a')
  })

  it('手动 next 在 repeat=one 时仍然换歌，自动播放结束才单曲循环', () => {
    const { player, audio } = setup()
    player.setRepeat('one')
    player.selectTrack('a')
    player.next()
    expect(player.currentTrack.id).toBe('b')

    audio.seek.mock?.clear?.()
    audio.calls.seek.length = 0
    player.handleEnded()
    expect(player.currentTrack.id).toBe('b')
    expect(audio.calls.seek).toEqual([0])
  })

  it('prev 从第一首环绕到最后一首', () => {
    const { player } = setup()
    player.selectTrack('a')
    player.prev()
    expect(player.currentTrack.id).toBe('c')
  })

  it('循环模式 off -> all -> one -> off', () => {
    const { player } = setup()
    player.cycleRepeat()
    expect(player.state.repeat).toBe('all')
    player.cycleRepeat()
    expect(player.state.repeat).toBe('one')
    player.cycleRepeat()
    expect(player.state.repeat).toBe('off')
  })

  it('随机模式下 next 不会停在当前曲目', () => {
    const { player } = setup({ random: () => 0 })
    player.toggleShuffle()
    expect(player.state.shuffle).toBe(true)
    player.selectTrack('b')
    player.next()
    expect(player.currentTrack.id).toBe('c')
  })

  it('音频自然结束后自动播放下一首', () => {
    const { player, audio } = setup()
    player.setRepeat('all')
    player.selectTrack('a')
    audio.end()
    expect(player.currentTrack.id).toBe('b')
    expect(player.state.isPlaying).toBe(true)
  })
})

describe('进度与音量', () => {
  it('timeupdate 事件同步进度，并可刷新真实时长', () => {
    const { player, audio } = setup()
    player.selectTrack('a')
    audio.advance(2.5)
    expect(player.state.currentTime).toBe(2.5)
    audio.ready(6.5)
    expect(player.state.duration).toBe(6.5)
    expect(player.progress).toBeCloseTo(2.5 / 6.5, 5)
  })

  it('seek 会夹紧到 [0, duration]', () => {
    const { player, audio } = setup()
    player.selectTrack('a')
    player.seek(4)
    expect(audio.calls.seek.at(-1)).toBe(4)
    player.seek(999)
    expect(player.state.currentTime).toBe(6)
    player.seek(-3)
    expect(player.state.currentTime).toBe(0)
  })

  it('seekRatio 按比例跳转', () => {
    const { player } = setup()
    player.selectTrack('c')
    player.seekRatio(0.5)
    expect(player.state.currentTime).toBe(5)
    player.seekRatio(2)
    expect(player.state.currentTime).toBe(10)
  })

  it('setVolume 夹紧音量，并在音量 > 0 时自动取消静音', () => {
    const { player, audio } = setup()
    player.toggleMute()
    expect(player.state.muted).toBe(true)
    player.setVolume(0.3)
    expect(player.state.volume).toBe(0.3)
    expect(player.state.muted).toBe(false)
    expect(audio.calls.setMuted.at(-1)).toBe(false)
    player.setVolume(5)
    expect(player.state.volume).toBe(1)
  })

  it('toggleMute 联动音频引擎', () => {
    const { player, audio } = setup()
    player.toggleMute()
    expect(audio.calls.setMuted.at(-1)).toBe(true)
    expect(player.state.muted).toBe(true)
  })
})

describe('搜索 / 收藏', () => {
  it('setQuery 过滤可见列表，clearQuery 复原', () => {
    const { player } = setup()
    player.setQuery('第二')
    expect(player.visibleTracks.map((t) => t.id)).toEqual(['b'])
    player.clearQuery()
    expect(player.visibleTracks).toHaveLength(3)
  })

  it('收藏可切换，并且能写进 storage', () => {
    const { player, storage } = setup()
    expect(player.isFavorite('a')).toBe(false)
    expect(player.toggleFavorite('a')).toBe(true)
    expect(player.state.favorites).toEqual(['a'])
    expect(JSON.parse(storage.getItem('lzx-music:favorites'))).toEqual(['a'])
    expect(player.toggleFavorite('a')).toBe(false)
    expect(player.state.favorites).toEqual([])
  })

  it('新实例能从 storage 恢复收藏', () => {
    const storage = createSafeStorage()
    const first = createPlayer({ tracks, audio: createFakeAudio(), storage })
    first.toggleFavorite('c')
    const second = createPlayer({ tracks, audio: createFakeAudio(), storage })
    expect(second.state.favorites).toEqual(['c'])
  })

  it('favoritesOnly 只显示收藏曲目，可叠加搜索条件', () => {
    const { player } = setup()
    player.toggleFavorite('a')
    player.toggleFavorite('c')
    player.toggleFavoritesOnly()
    expect(player.visibleTracks.map((t) => t.id)).toEqual(['a', 'c'])
    player.setQuery('第三')
    expect(player.visibleTracks.map((t) => t.id)).toEqual(['c'])
  })

  it('storage 抛异常时收藏功能仍可用', () => {
    const storage = {
      getItem: () => {
        throw new Error('boom')
      },
      setItem: () => {
        throw new Error('boom')
      },
    }
    const player = createPlayer({ tracks, audio: createFakeAudio(), storage })
    expect(player.state.favorites).toEqual([])
    expect(player.toggleFavorite('a')).toBe(true)
  })
})

describe('异常与销毁', () => {
  it('音频 error 事件会写入 state.error 并停止播放', () => {
    const { player, audio } = setup()
    player.selectTrack('a')
    audio.fail('解码失败')
    expect(player.state.error).toBe('解码失败')
    expect(player.state.isPlaying).toBe(false)
  })

  it('load 抛错时给出错误信息', () => {
    const audio = createFakeAudio()
    audio.load = () => {
      throw new Error('不支持的音源')
    }
    const player = createPlayer({ tracks, audio, storage: createSafeStorage() })
    player.selectTrack('a')
    expect(player.state.error).toBe('不支持的音源')
    expect(player.state.isPlaying).toBe(false)
  })

  it('dispose 会暂停并释放引擎', () => {
    const { player, audio } = setup()
    player.selectTrack('a')
    player.dispose()
    expect(player.state.isPlaying).toBe(false)
    expect(audio.disposed).toBe(true)
  })

  it('空歌单不会崩溃', () => {
    const player = createPlayer({ tracks: [], audio: createFakeAudio(), storage: createSafeStorage() })
    expect(player.play()).toBe(false)
    expect(player.next()).toBe(false)
    expect(player.prev()).toBe(false)
    expect(player.currentTrack).toBeNull()
  })
})

beforeEach(() => {
  localStorage.clear()
})
