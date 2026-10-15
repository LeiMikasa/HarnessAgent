/**
 * 整页集成测试：真实挂载 App.vue（含所有子组件 + 单例 player），
 * 用 DOM 事件模拟用户操作，验证「搜索 → 选歌 → 播放 → 收藏 → 快捷键」整条链路。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import App from '../src/App.vue'
import { usePlayer } from '../src/composables/usePlayer.js'

const player = usePlayer()

let wrapper

function mountApp() {
  wrapper = mount(App, { attachTo: document.body })
  return wrapper
}

beforeEach(() => {
  player.pause()
  player.clearQuery()
  player.setRepeat('off')
  if (player.state.shuffle) player.toggleShuffle()
  if (player.state.favoritesOnly) player.toggleFavoritesOnly()
  player.state.favorites.splice(0, player.state.favorites.length)
  player.state.error = null
})

describe('页面骨架', () => {
  it('渲染标题、歌单与播放条', () => {
    mountApp()
    expect(wrapper.get('[data-test="app-title"]').text()).toBe('LZX 音乐台')
    expect(wrapper.findAll('[data-test="track-item"]').length).toBe(player.state.tracks.length)
    expect(wrapper.find('[data-test="player-bar"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="now-playing-title"]').text()).toBe('未选择歌曲')
    wrapper.unmount()
  })

  it('列表摘要显示曲目数量与总时长', () => {
    mountApp()
    expect(wrapper.get('[data-test="list-summary"]').text()).toMatch(/8 首 · 共/)
    wrapper.unmount()
  })
})

describe('搜索', () => {
  it('输入关键字后只留下匹配的曲目', async () => {
    mountApp()
    const input = wrapper.get('[data-test="search-input"]')
    await input.setValue('夜航')
    expect(wrapper.findAll('[data-test="track-item"]').length).toBe(1)
    expect(wrapper.get('[data-test="track-title"]').text()).toBe('夜航西飞')
    expect(wrapper.get('[data-test="search-count"]').text()).toBe('显示 1 / 8 首')
    wrapper.unmount()
  })

  it('无结果时显示空状态', async () => {
    mountApp()
    await wrapper.get('[data-test="search-input"]').setValue('不存在的歌曲')
    expect(wrapper.get('[data-test="track-list-empty"]').text()).toBe('没有匹配的歌曲')
    wrapper.unmount()
  })

  it('点击清除按钮恢复完整歌单', async () => {
    mountApp()
    await wrapper.get('[data-test="search-input"]').setValue('夜航')
    await wrapper.get('[data-test="search-clear"]').trigger('click')
    expect(wrapper.findAll('[data-test="track-item"]').length).toBe(player.state.tracks.length)
    wrapper.unmount()
  })
})

describe('播放链路', () => {
  it('点击曲目即可播放，播放条同步显示歌名/歌手/时长', async () => {
    mountApp()
    await wrapper.findAll('[data-test="track-item"] .track-main')[0].trigger('click')
    expect(player.state.isPlaying).toBe(true)
    expect(player.currentTrack.title).toBe('司空震的野区')
    expect(wrapper.get('[data-test="now-playing-title"]').text()).toBe('司空震的野区')
    expect(wrapper.get('[data-test="now-playing-artist"]').text()).toBe('一万二司空震')
    expect(wrapper.get('[data-test="progress-duration"]').text()).toBe('0:24')
    expect(wrapper.get('[data-test="play-toggle"]').attributes('aria-label')).toBe('暂停')
    wrapper.unmount()
  })

  it('播放/暂停按钮可切换状态', async () => {
    mountApp()
    await wrapper.get('[data-test="play-toggle"]').trigger('click')
    expect(player.state.isPlaying).toBe(true)
    await wrapper.get('[data-test="play-toggle"]').trigger('click')
    expect(player.state.isPlaying).toBe(false)
    expect(wrapper.get('[data-test="play-toggle"]').attributes('aria-label')).toBe('播放')
    wrapper.unmount()
  })

  it('下一首 / 上一首按钮切换曲目', async () => {
    mountApp()
    await wrapper.findAll('[data-test="track-item"] .track-main')[0].trigger('click')
    await wrapper.get('[data-test="next"]').trigger('click')
    expect(player.currentTrack.title).toBe('雷智翔的早晨')
    await wrapper.get('[data-test="prev"]').trigger('click')
    expect(player.currentTrack.title).toBe('司空震的野区')
    wrapper.unmount()
  })

  it('当前播放的曲目带 data-active / data-playing 标记', async () => {
    mountApp()
    await wrapper.findAll('[data-test="track-item"] .track-main')[1].trigger('click')
    const items = wrapper.findAll('[data-test="track-item"]')
    expect(items[1].attributes('data-active')).toBe('true')
    expect(items[1].attributes('data-playing')).toBe('true')
    expect(items[0].attributes('data-active')).toBe('false')
    wrapper.unmount()
  })

  it('拖动进度条会 seek 到对应秒数', async () => {
    mountApp()
    await wrapper.findAll('[data-test="track-item"] .track-main')[0].trigger('click')
    const range = wrapper.get('[data-test="progress-range"]')
    range.element.value = '50'
    await range.trigger('input')
    expect(player.state.currentTime).toBe(12) // 24 秒的一半
    expect(wrapper.get('[data-test="progress-fill"]').attributes('style')).toContain('50%')
    wrapper.unmount()
  })

  it('调节音量与静音按钮生效', async () => {
    mountApp()
    const range = wrapper.get('[data-test="volume-range"]')
    range.element.value = '30'
    await range.trigger('input')
    expect(player.state.volume).toBeCloseTo(0.3, 5)
    await wrapper.get('[data-test="volume-mute"]').trigger('click')
    expect(player.state.muted).toBe(true)
    expect(wrapper.get('[data-test="volume-value"]').text()).toBe('静音')
    wrapper.unmount()
  })

  it('循环按钮循环切换 off → all → one', async () => {
    mountApp()
    const btn = wrapper.get('[data-test="repeat-toggle"]')
    await btn.trigger('click')
    expect(btn.attributes('data-repeat')).toBe('all')
    await btn.trigger('click')
    expect(btn.attributes('data-repeat')).toBe('one')
    await btn.trigger('click')
    expect(btn.attributes('data-repeat')).toBe('off')
    wrapper.unmount()
  })

  it('随机按钮切换 shuffle', async () => {
    mountApp()
    const btn = wrapper.get('[data-test="shuffle-toggle"]')
    await btn.trigger('click')
    expect(player.state.shuffle).toBe(true)
    expect(btn.attributes('data-active')).toBe('true')
    wrapper.unmount()
  })
})

describe('收藏', () => {
  it('点小心心切换收藏状态并持久化', async () => {
    mountApp()
    const btn = wrapper.findAll('[data-test="favorite-btn"]')[0]
    await btn.trigger('click')
    expect(btn.attributes('data-active')).toBe('true')
    expect(player.state.favorites).toEqual([player.state.tracks[0].id])
    expect(localStorage.getItem('lzx-music:favorites')).toContain(player.state.tracks[0].id)
    await btn.trigger('click')
    expect(player.state.favorites).toEqual([])
    wrapper.unmount()
  })

  it('「只看收藏」只显示收藏曲目', async () => {
    mountApp()
    await wrapper.findAll('[data-test="favorite-btn"]')[2].trigger('click')
    await wrapper.get('[data-test="favorites-only"]').trigger('click')
    expect(wrapper.findAll('[data-test="track-item"]').length).toBe(1)
    expect(wrapper.get('[data-test="track-title"]').text()).toBe('像素恋人')
    await wrapper.get('[data-test="favorites-only"]').trigger('click')
    expect(wrapper.findAll('[data-test="track-item"]').length).toBe(player.state.tracks.length)
    wrapper.unmount()
  })
})

describe('键盘快捷键', () => {
  function press(key) {
    window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }))
  }

  it('空格播放/暂停，左右方向键切歌', async () => {
    mountApp()
    press(' ')
    expect(player.state.isPlaying).toBe(true)
    press(' ')
    expect(player.state.isPlaying).toBe(false)
    press('ArrowRight')
    expect(player.currentTrack).not.toBeNull()
    const afterNext = player.currentTrack.id
    press('ArrowLeft')
    expect(player.currentTrack.id).not.toBe(afterNext)
    wrapper.unmount()
  })

  it('上下方向键调整音量，M 静音，S 随机，R 循环', () => {
    mountApp()
    const volume = player.state.volume
    press('ArrowUp')
    expect(player.state.volume).toBeGreaterThan(volume)
    press('ArrowDown')
    expect(player.state.volume).toBeCloseTo(volume, 5)
    press('m')
    expect(player.state.muted).toBe(true)
    press('s')
    expect(player.state.shuffle).toBe(true)
    press('r')
    expect(player.state.repeat).toBe('all')
    wrapper.unmount()
  })

  it('/ 聚焦搜索框，Esc 清空搜索', async () => {
    mountApp()
    press('/')
    expect(document.activeElement?.getAttribute('data-test')).toBe('search-input')
    await wrapper.get('[data-test="search-input"]').setValue('夜航')
    press('Escape')
    expect(player.state.query).toBe('')
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('[data-test="track-item"]').length).toBe(player.state.tracks.length)
    wrapper.unmount()
  })

  it('在输入框内打字不会触发播放快捷键', async () => {
    mountApp()
    const input = wrapper.get('[data-test="search-input"]')
    await input.trigger('focus')
    input.element.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))
    expect(player.state.isPlaying).toBe(false)
    wrapper.unmount()
  })
})

describe('异常提示', () => {
  it('state.error 非空时展示错误横幅', async () => {
    mountApp()
    player.state.error = '音频加载失败'
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-test="error-banner"]').text()).toContain('音频加载失败')
    player.state.error = null
    wrapper.unmount()
  })
})
