/**
 * 音频链路自检（Node 侧，不经浏览器）：`npm run check:audio`
 *
 * 把每首曲子的 `tone:*` 虚拟音源渲染成 WAV，校验 RIFF 头、时长、峰值与 RMS，
 * 证明「点播放真的会出声」，而不是静音占位。
 * 退出码 0 = 全部正常，1 = 有曲目异常（可直接接进 CI）。
 */
import { parseToneSrc, renderToneWav } from '../src/utils/tone.js'
import { tracks } from '../src/data/tracks.js'

let bad = 0
console.log(`曲目数：${tracks.length}`)

for (const track of tracks) {
  const spec = parseToneSrc(track.src)
  if (!spec) {
    console.log(`✗ ${track.title}：音源不是可合成的 tone: 描述符 -> ${track.src}`)
    bad += 1
    continue
  }

  const bytes = renderToneWav({ ...spec, seed: 42 })
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const sampleRate = view.getUint32(24, true)
  const bitsPerSample = view.getUint16(34, true)
  const dataBytes = view.getUint32(40, true)
  const seconds = dataBytes / (sampleRate * (bitsPerSample / 8))

  let peak = 0
  let sumOfSquares = 0
  const sampleCount = dataBytes / 2
  for (let i = 44; i + 1 < bytes.length; i += 2) {
    const sample = ((bytes[i] | (bytes[i + 1] << 8)) << 16) >> 16
    peak = Math.max(peak, Math.abs(sample))
    sumOfSquares += sample * sample
  }
  const rms = Math.sqrt(sumOfSquares / sampleCount)

  const ok =
    String.fromCharCode(...bytes.subarray(0, 4)) === 'RIFF' &&
    Math.abs(seconds - track.duration) < 0.05 &&
    peak > 1000 &&
    rms > 500
  if (!ok) bad += 1

  console.log(
    `${ok ? '✓' : '✗'} ${track.title.padEnd(10)} ${track.artist.padEnd(12)} ` +
      `基频=${String(spec.freq).padStart(4)}Hz 时长=${seconds.toFixed(2)}s/${track.duration}s ` +
      `峰值=${peak} RMS=${rms.toFixed(0)} 体积=${(bytes.length / 1024).toFixed(0)}KB`,
  )
}

if (bad) {
  console.error(`\n✗ ${bad} 首曲子的音频链路异常`)
  process.exit(1)
}
console.log('\n✓ 全部曲目的 WAV 合成链路正常（有波形，且时长与曲目元数据一致）')
