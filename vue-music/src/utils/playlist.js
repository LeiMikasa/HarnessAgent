/**
 * 歌单相关的纯逻辑（搜索过滤 + 上一首/下一首索引计算）。
 * 这些函数不依赖 Vue，也不触碰 DOM，方便直接单测。
 */

/** 单曲循环 / 列表循环 / 顺序播放 */
export const REPEAT_MODES = ['off', 'all', 'one']

/**
 * 大小写不敏感地在 标题/歌手/专辑/标签 中搜索。
 * 空查询返回原数组的浅拷贝（保持调用方不可变性）。
 * @param {Array<object>} tracks
 * @param {string} query
 * @returns {Array<object>}
 */
export function filterTracks(tracks = [], query = '') {
  const q = String(query ?? '').trim().toLowerCase()
  if (!q) return tracks.slice()
  return tracks.filter((track) => {
    if (!track) return false
    const haystack = [track.title, track.artist, track.album, ...(track.tags ?? [])]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
    return haystack.includes(q)
  })
}

/** 只保留收藏的曲目 */
export function filterFavorites(tracks = [], favorites = []) {
  const set = new Set(favorites)
  return tracks.filter((track) => set.has(track?.id))
}

/**
 * 计算下一首的索引。
 * @param {number} current 当前索引，-1 表示尚未选曲
 * @param {number} length 歌单长度
 * @param {{shuffle?: boolean, repeat?: 'off'|'all'|'one', random?: () => number}} [options]
 * @returns {number} 目标索引；返回 -1 表示播放应该停止
 */
export function nextIndex(current, length, options = {}) {
  const { shuffle = false, repeat = 'off', random = Math.random } = options
  if (!Number.isInteger(length) || length <= 0) return -1
  if (!Number.isInteger(current) || current < 0) return 0
  if (shuffle) {
    if (length === 1) return 0
    // 随机但不重复当前曲目
    return (current + shuffleOffset(length, random)) % length
  }
  if (repeat === 'one') return current
  const next = current + 1
  if (next < length) return next
  return repeat === 'all' ? 0 : -1
}

/**
 * 计算上一首的索引（手动切歌时总是环绕）。
 * @param {number} current
 * @param {number} length
 * @param {{shuffle?: boolean, random?: () => number}} [options]
 * @returns {number} -1 表示歌单为空
 */
export function prevIndex(current, length, options = {}) {
  const { shuffle = false, random = Math.random } = options
  if (!Number.isInteger(length) || length <= 0) return -1
  if (!Number.isInteger(current) || current < 0) return 0
  if (shuffle) {
    if (length === 1) return 0
    const offset = shuffleOffset(length, random)
    return (current - offset + length * 2) % length
  }
  return (current - 1 + length) % length
}

/** 洗牌模式下的跳跃步长，取值区间 [1, length-1]，保证不会停在当前曲目 */
function shuffleOffset(length, random) {
  const span = length - 1
  const step = Math.min(span - 1, Math.floor(clamp01(random()) * span))
  return 1 + Math.max(0, step)
}

/** off -> all -> one -> off；未知取值按 off 处理（回到 all） */
export function cycleRepeatMode(mode) {
  const index = REPEAT_MODES.indexOf(mode)
  const current = index < 0 ? 0 : index
  return REPEAT_MODES[(current + 1) % REPEAT_MODES.length]
}

/** 在歌单中按 id 找索引，找不到返回 -1 */
export function indexOfId(tracks = [], id) {
  return tracks.findIndex((track) => track?.id === id)
}

/** 把任意值收敛到 [0,1] */
export function clamp01(value) {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}
