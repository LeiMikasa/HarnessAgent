/**
 * 通用格式化工具（纯函数，无副作用，便于单测）。
 */

/**
 * 把秒数格式化为 `m:ss` 或 `h:mm:ss`。
 * 非法输入（NaN / Infinity / 负数）统一返回 '0:00'。
 * @param {number} seconds
 * @returns {string}
 */
export function formatTime(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '0:00'
  const total = Math.floor(seconds)
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  const pad = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
}

/**
 * 播放进度百分比（0-100 的整数），用于进度条宽度。
 */
export function formatPercent(value, total) {
  if (!Number.isFinite(value) || !Number.isFinite(total) || total <= 0) return 0
  return Math.min(100, Math.max(0, Math.round((value / total) * 100)))
}

/**
 * 歌单总时长文案，如 "38 分 12 秒"。
 */
export function formatTotalDuration(tracks = []) {
  const total = tracks.reduce((sum, t) => sum + (Number(t?.duration) || 0), 0)
  const minutes = Math.floor(total / 60)
  const seconds = Math.floor(total % 60)
  if (minutes <= 0) return `${seconds} 秒`
  return `${minutes} 分 ${seconds} 秒`
}

/**
 * 0.42 -> "42%"
 */
export function formatVolume(volume) {
  if (!Number.isFinite(volume)) return '0%'
  return `${Math.round(Math.min(1, Math.max(0, volume)) * 100)}%`
}
