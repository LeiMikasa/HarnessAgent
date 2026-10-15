/**
 * 程序化音频生成：把 `tone:<基频>:<秒数>` 这样的虚拟音源转成 WAV data URL。
 *
 * 这样做的好处是整站零外部资源：不依赖任何 CDN / 在线 mp3，
 * 打开页面就能真正听到声音（一段用五声音阶生成的循环旋律）。
 */

const DEFAULT_SAMPLE_RATE = 22050

/** 五声音阶（相对半音），听起来比较"国风"，符合音乐台气质 */
const PENTATONIC = [0, 2, 4, 7, 9, 12, 14, 16]

/**
 * 解析虚拟音源描述符。
 * @param {string} src 形如 `tone:392:18`（基频 Hz : 时长秒）；非法值返回 null
 * @returns {{freq: number, seconds: number} | null}
 */
export function parseToneSrc(src) {
  if (typeof src !== 'string') return null
  const match = /^tone:(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(src.trim())
  if (!match) return null
  const freq = Number(match[1])
  const seconds = Number(match[2])
  if (!Number.isFinite(freq) || !Number.isFinite(seconds) || freq <= 0 || seconds <= 0) return null
  return { freq, seconds }
}

/** 确定性伪随机（同一个 seed 永远得到同一段旋律，方便测试与缓存） */
function mulberry32(seed) {
  let a = seed >>> 0
  return function random() {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/**
 * 生成一段 16bit 单声道 PCM WAV。
 * @param {{freq?: number, seconds?: number, sampleRate?: number, seed?: number}} [options]
 * @returns {Uint8Array}
 */
export function renderToneWav(options = {}) {
  const freq = Number(options.freq) > 0 ? Number(options.freq) : 440
  const seconds = Number(options.seconds) > 0 ? Number(options.seconds) : 12
  const sampleRate = Number(options.sampleRate) > 0 ? Number(options.sampleRate) : DEFAULT_SAMPLE_RATE
  const seed = Number.isFinite(options.seed) ? Number(options.seed) : Math.round(freq)
  const random = mulberry32(seed)

  const totalSamples = Math.floor(seconds * sampleRate)
  const noteLength = 0.42 * sampleRate // 每个音符约 0.42s
  const noteCount = Math.max(1, Math.round(totalSamples / noteLength))
  // 先排好音符，保证旋律不会在 buffer 边界处"跳音"
  const notes = Array.from({ length: noteCount }, () => {
    const degree = PENTATONIC[Math.floor(random() * PENTATONIC.length)]
    return freq * Math.pow(2, degree / 12)
  })

  const data = new Uint8Array(totalSamples * 2)
  const view = new DataView(data.buffer)
  for (let i = 0; i < totalSamples; i += 1) {
    const t = i / sampleRate
    const noteIndex = Math.min(notes.length - 1, Math.floor(i / noteLength))
    const noteFreq = notes[noteIndex]
    const localT = (i % noteLength) / sampleRate
    // 简易 ADSR：起音 8ms，之后指数衰减，音符尾部快速收干
    const attack = Math.min(1, localT / 0.008)
    const release = Math.max(0, 1 - localT / (noteLength / sampleRate))
    const envelope = attack * Math.pow(release, 1.6)
    // 基频 + 两个泛音，再加一点颤音，声音更接近"乐器"而不是纯正弦
    const vibrato = 1 + 0.0035 * Math.sin(2 * Math.PI * 5.5 * t)
    const phase = 2 * Math.PI * noteFreq * vibrato * t
    const sample =
      0.6 * Math.sin(phase) +
      0.25 * Math.sin(2 * phase) +
      0.12 * Math.sin(3 * phase) +
      0.05 * Math.sin(phase * 0.5)
    // 淡入淡出整段音频，避免开头/结尾爆音
    const fade = Math.min(1, t / 0.05, (seconds - t) / 0.25)
    const value = Math.max(-1, Math.min(1, sample * envelope * fade * 0.55))
    view.setInt16(i * 2, Math.round(value * 32767), true)
  }

  return new Uint8Array(concatHeader(data, sampleRate))
}

function concatHeader(pcm, sampleRate) {
  const header = new Uint8Array(44)
  const view = new DataView(header.buffer)
  const byteRate = sampleRate * 2
  writeAscii(header, 0, 'RIFF')
  view.setUint32(4, 36 + pcm.length, true)
  writeAscii(header, 8, 'WAVE')
  writeAscii(header, 12, 'fmt ')
  view.setUint32(16, 16, true) // PCM 子块大小
  view.setUint16(20, 1, true) // PCM 格式
  view.setUint16(22, 1, true) // 单声道
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, byteRate, true)
  view.setUint16(32, 2, true) // 块对齐
  view.setUint16(34, 16, true) // 位深
  writeAscii(header, 36, 'data')
  view.setUint32(40, pcm.length, true)
  const out = new Uint8Array(header.length + pcm.length)
  out.set(header, 0)
  out.set(pcm, 44)
  return out.buffer
}

function writeAscii(bytes, offset, text) {
  for (let i = 0; i < text.length; i += 1) bytes[offset + i] = text.charCodeAt(i)
}

/** 浏览器与 Node(jsdom) 都能用的 base64 编码 */
function toBase64(bytes) {
  const CHUNK = 0x8000
  if (typeof btoa === 'function') {
    let binary = ''
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK))
    }
    return btoa(binary)
  }
  if (typeof Buffer !== 'undefined') return Buffer.from(bytes).toString('base64')
  throw new Error('当前环境不支持 base64 编码')
}

/**
 * 把虚拟音源转成可直接喂给 <audio> 的 data URL。
 * @param {string} src `tone:freq:seconds`
 * @returns {string}
 */
export function makeToneDataUrl(src) {
  const spec = parseToneSrc(src)
  if (!spec) throw new Error(`无法识别的虚拟音源: ${src}`)
  return `data:audio/wav;base64,${toBase64(renderToneWav(spec))}`
}
