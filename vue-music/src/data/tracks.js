/**
 * 内置歌单。
 *
 * src 使用虚拟音源 `tone:<基频Hz>:<时长秒>`：播放时前端现场合成 WAV，
 * 因此整站零外部资源（不用联网、不用放 mp3 文件）也能真正出声。
 * 若以后要接真实歌曲，把 src 换成 http(s) 或 /assets/xxx.mp3 即可。
 */
export const tracks = [
  {
    id: 'lzx-001',
    title: '司空震的野区',
    artist: '一万二司空震',
    album: '峡谷回声',
    cover: '⚡',
    color: 'linear-gradient(135deg, #7c3aed, #2563eb)',
    src: 'tone:392:24',
    duration: 24,
    tags: ['王者荣耀', '电音'],
  },
  {
    id: 'lzx-002',
    title: '雷智翔的早晨',
    artist: 'Harness 乐队',
    album: 'D 盘清晨',
    cover: '🌅',
    color: 'linear-gradient(135deg, #f59e0b, #ef4444)',
    src: 'tone:440:28',
    duration: 28,
    tags: ['民谣', '轻快'],
  },
  {
    id: 'lzx-003',
    title: '像素恋人',
    artist: 'Vue 三人组',
    album: '响应式',
    cover: '💜',
    color: 'linear-gradient(135deg, #a855f7, #ec4899)',
    src: 'tone:349:22',
    duration: 22,
    tags: ['City Pop', '合成器'],
  },
  {
    id: 'lzx-004',
    title: '响应式心跳',
    artist: 'Reactive',
    album: '响应式',
    cover: '💓',
    color: 'linear-gradient(135deg, #06b6d4, #3b82f6)',
    src: 'tone:523:20',
    duration: 20,
    tags: ['电子'],
  },
  {
    id: 'lzx-005',
    title: '夜航西飞',
    artist: '阿翔',
    album: '深夜频道',
    cover: '🌙',
    color: 'linear-gradient(135deg, #1e293b, #6366f1)',
    src: 'tone:294:32',
    duration: 32,
    tags: ['氛围', '助眠'],
  },
  {
    id: 'lzx-006',
    title: '袁野的吉他',
    artist: '袁野',
    album: '小小演奏家',
    cover: '🎸',
    color: 'linear-gradient(135deg, #10b981, #84cc16)',
    src: 'tone:330:26',
    duration: 26,
    tags: ['吉他', '亲子'],
  },
  {
    id: 'lzx-007',
    title: '代码与咖啡',
    artist: 'Harness 乐队',
    album: 'D 盘清晨',
    cover: '☕',
    color: 'linear-gradient(135deg, #78350f, #d97706)',
    src: 'tone:466:18',
    duration: 18,
    tags: ['Lo-fi', '工作'],
  },
  {
    id: 'lzx-008',
    title: '长城谣 · 变奏',
    artist: '民乐小组',
    album: '国风集',
    cover: '🏯',
    color: 'linear-gradient(135deg, #dc2626, #f97316)',
    src: 'tone:261:30',
    duration: 30,
    tags: ['国风', '古筝'],
  },
]

export default tracks
