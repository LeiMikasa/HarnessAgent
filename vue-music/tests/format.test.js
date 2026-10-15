import { describe, expect, it } from 'vitest'
import { formatPercent, formatTime, formatTotalDuration, formatVolume } from '../src/utils/format.js'

describe('formatTime', () => {
  it('把秒格式化为 m:ss', () => {
    expect(formatTime(0)).toBe('0:00')
    expect(formatTime(9)).toBe('0:09')
    expect(formatTime(65)).toBe('1:05')
    expect(formatTime(599)).toBe('9:59')
  })

  it('超过一小时使用 h:mm:ss', () => {
    expect(formatTime(3600)).toBe('1:00:00')
    expect(formatTime(3725)).toBe('1:02:05')
  })

  it('小数秒向下取整', () => {
    expect(formatTime(12.87)).toBe('0:12')
  })

  it('非法输入回退到 0:00', () => {
    expect(formatTime(NaN)).toBe('0:00')
    expect(formatTime(-5)).toBe('0:00')
    expect(formatTime(Infinity)).toBe('0:00')
    expect(formatTime(undefined)).toBe('0:00')
    expect(formatTime('12')).toBe('0:00')
  })
})

describe('formatPercent', () => {
  it('返回 0-100 的整数百分比', () => {
    expect(formatPercent(30, 120)).toBe(25)
    expect(formatPercent(1, 3)).toBe(33)
  })

  it('越界会被夹紧', () => {
    expect(formatPercent(500, 100)).toBe(100)
    expect(formatPercent(-10, 100)).toBe(0)
  })

  it('总时长为 0 或非法时返回 0', () => {
    expect(formatPercent(10, 0)).toBe(0)
    expect(formatPercent(10, NaN)).toBe(0)
    expect(formatPercent(NaN, 10)).toBe(0)
  })
})

describe('formatTotalDuration', () => {
  it('汇总歌单时长', () => {
    expect(formatTotalDuration([{ duration: 60 }, { duration: 120 }])).toBe('3 分 0 秒')
    expect(formatTotalDuration([{ duration: 45 }])).toBe('45 秒')
    expect(formatTotalDuration([{ duration: 90 }, { duration: 35 }])).toBe('2 分 5 秒')
  })

  it('空数组与脏数据不炸', () => {
    expect(formatTotalDuration([])).toBe('0 秒')
    expect(formatTotalDuration(undefined)).toBe('0 秒')
    expect(formatTotalDuration([{ duration: 'abc' }, { duration: 30 }])).toBe('30 秒')
  })
})

describe('formatVolume', () => {
  it('输出百分比文案', () => {
    expect(formatVolume(0.8)).toBe('80%')
    expect(formatVolume(0)).toBe('0%')
    expect(formatVolume(1)).toBe('100%')
  })

  it('越界与非法值', () => {
    expect(formatVolume(1.5)).toBe('100%')
    expect(formatVolume(-1)).toBe('0%')
    expect(formatVolume(NaN)).toBe('0%')
  })
})
