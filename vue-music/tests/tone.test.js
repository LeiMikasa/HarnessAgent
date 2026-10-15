import { describe, expect, it } from 'vitest'
import { makeToneDataUrl, parseToneSrc, renderToneWav } from '../src/utils/tone.js'

function ascii(bytes, offset, length) {
  return String.fromCharCode(...bytes.subarray(offset, offset + length))
}

describe('parseToneSrc', () => {
  it('解析合法音源描述符', () => {
    expect(parseToneSrc('tone:440:12')).toEqual({ freq: 440, seconds: 12 })
    expect(parseToneSrc(' tone:349.5:18 ')).toEqual({ freq: 349.5, seconds: 18 })
  })

  it('非法描述符返回 null', () => {
    expect(parseToneSrc('tone:0:12')).toBeNull()
    expect(parseToneSrc('tone:440:0')).toBeNull()
    expect(parseToneSrc('tone:abc:12')).toBeNull()
    expect(parseToneSrc('https://example.com/a.mp3')).toBeNull()
    expect(parseToneSrc('')).toBeNull()
    expect(parseToneSrc(undefined)).toBeNull()
  })
})

describe('renderToneWav', () => {
  it('生成标准 44 字节头 + 16bit 单声道 PCM', () => {
    const bytes = renderToneWav({ freq: 440, seconds: 1, sampleRate: 8000, seed: 1 })
    expect(ascii(bytes, 0, 4)).toBe('RIFF')
    expect(ascii(bytes, 8, 4)).toBe('WAVE')
    expect(ascii(bytes, 12, 4)).toBe('fmt ')
    expect(ascii(bytes, 36, 4)).toBe('data')
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    expect(view.getUint16(20, true)).toBe(1) // PCM
    expect(view.getUint16(22, true)).toBe(1) // 单声道
    expect(view.getUint32(24, true)).toBe(8000) // 采样率
    expect(view.getUint16(34, true)).toBe(16) // 位深
    expect(bytes.length).toBe(44 + 8000 * 2)
    expect(view.getUint32(40, true)).toBe(8000 * 2)
    expect(view.getUint32(4, true)).toBe(36 + 8000 * 2)
  })

  it('同样的参数生成完全一致的字节（可缓存）', () => {
    const a = renderToneWav({ freq: 392, seconds: 1, seed: 7 })
    const b = renderToneWav({ freq: 392, seconds: 1, seed: 7 })
    expect(Array.from(a)).toEqual(Array.from(b))
  })

  it('不同基频生成不同内容，且样本不为静音', () => {
    const a = renderToneWav({ freq: 392, seconds: 1, seed: 7 })
    const b = renderToneWav({ freq: 523, seconds: 1, seed: 7 })
    expect(Array.from(a)).not.toEqual(Array.from(b))
    const peak = Math.max(...Array.from(a.subarray(44, 500), (v) => Math.abs(v - 128)))
    expect(peak).toBeGreaterThan(0)
  })

  it('非法时长退化为默认值而不是抛错', () => {
    expect(() => renderToneWav({ freq: 440, seconds: -3 })).not.toThrow()
    expect(renderToneWav({ seconds: 0.5, sampleRate: 2000 }).length).toBeGreaterThan(44)
  })
})

describe('makeToneDataUrl', () => {
  it('输出可被浏览器识别的 data URL', () => {
    const url = makeToneDataUrl('tone:440:0.5')
    expect(url.startsWith('data:audio/wav;base64,')).toBe(true)
    expect(url.length).toBeGreaterThan(100)
  })

  it('非法音源抛错', () => {
    expect(() => makeToneDataUrl('tone:oops:1')).toThrow()
  })
})
