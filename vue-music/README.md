# LZX 音乐台 · Vue 版

基于 **Vue 3 + Vite** 的单页音乐播放器前端页面。零外部资源依赖：不联网、不下载 mp3，歌单里的曲目用**现场合成的 WAV 音频源**（`tone:<基频Hz>:<时长秒>`）发声，因此在纯离线环境里点播放也**真的会出声**。

- 框架：Vue 3（`<script setup>` 组合式 API）
- 构建：Vite 6
- 测试：Vitest 2 + @vue/test-utils + jsdom
- 依赖：运行时仅 `vue`；无 UI 库、无图标库、无音频文件

## 快速开始

```bash
cd vue-music
npm install
npm run dev        # 开发服务器 http://localhost:5173
```

其他命令：

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 启动 Vite 开发服务器 |
| `npm test` | 全量单测（`vitest run`，一次跑完） |
| `npm run test:watch` | 监听模式跑单测 |
| `npm run build` | 生产构建，产物在 `dist/` |
| `npm run preview` | 预览构建产物（http://localhost:4173） |
| `npm run check:audio` | Node 侧音频链路自检：把 8 首歌的 `tone:` 源渲染成 WAV，校验 RIFF 头、时长、峰值与 RMS；异常时退出码 1，可直接接 CI |

## 页面功能

- **歌单列表**：内置 8 首曲目（标题 / 歌手 / 专辑 / 封面 / 时长 / 标签），显示当前播放高亮与"正在播放"标记。
- **搜索**：按标题、歌手、专辑、标签模糊过滤；计数条显示「显示 N / M 首」。
- **收藏**：每行收藏按钮，可切换"仅看收藏"视图，收藏状态持久化到 `localStorage`。
- **播放控制**：播放/暂停、上一首/下一首、随机播放、循环模式（顺序 / 列表循环 / 单曲循环）。
- **进度与音量**：可拖拽进度条跳转，音量条 + 一键静音。
- **键盘快捷键**：`空格` 播放/暂停 · `←`/`→` 上一首/下一首 · `↑`/`↓` 音量 · `M` 静音 · `S` 随机 · `R` 循环 · `/` 聚焦搜索 · `Esc` 清空搜索。

## 目录结构

```
vue-music/
├── index.html                  # 入口 HTML（内联 SVG favicon）
├── vite.config.js              # Vite + Vitest 配置（jsdom、@ 别名）
├── CONTRACT.md                 # 组件接口契约（props / emits / DOM 钩子），唯一事实来源
├── scripts/check-audio.mjs     # WAV 合成链路自检脚本（npm run check:audio）
├── src/
│   ├── main.js                 # 应用挂载入口
│   ├── App.vue                 # 页面装配 + 键盘快捷键 + localStorage 持久化
│   ├── data/tracks.js          # 内置歌单（8 首，src 为 tone: 虚拟音频源）
│   ├── styles/main.css         # 全局设计令牌（CSS 变量）
│   ├── composables/usePlayer.js# 播放器状态机（搜索/收藏/播放/音量/随机/循环），可注入依赖便于测试
│   ├── utils/
│   │   ├── format.js           # formatTime / formatVolume / formatPercent 等纯函数
│   │   ├── playlist.js         # 过滤、洗牌、按循环模式取下一首等纯逻辑
│   │   ├── tone.js             # tone: 源解析 + WAV 渲染（16-bit PCM）
│   │   └── audio.js            # Audio 元素封装（播放/暂停/音量/进度），可被假实现替换
│   └── components/             # 纯展示组件：props 进、emits 出，不碰播放器与 localStorage
│       ├── SearchBar.vue
│       ├── TrackList.vue
│       ├── ProgressBar.vue
│       ├── VolumeControl.vue
│       └── PlayerBar.vue
└── tests/                      # Vitest 单测
    ├── setup.js                # 全局测试环境（补齐 HTMLMediaElement 桩实现、每例清空 localStorage）
    ├── helpers/fake-audio.js   # 假 Audio 元素，避免真实播放
    ├── format.test.js  playlist.test.js  tone.test.js  audio.test.js
    ├── player.test.js          # 播放器状态机（含边界：空列表、NaN、越界）
    ├── app.test.js             # App 集成测试（含键盘快捷键、持久化）
    ├── search-bar.test.js  track-list.test.js
    └── progress-bar.test.js  volume-control.test.js  player-bar.test.js
```

## 测试策略

- **分层**：纯逻辑（`utils/*`）→ 状态机（`usePlayer`，注入假 Audio / 内存 storage）→ 纯展示组件（`@vue/test-utils` mount）→ App 集成（真实组件树 + 假 Audio）。
- **不依赖真实播放**：所有涉及音频的用例使用 `tests/helpers/fake-audio.js`，不 new Audio、不出声、不联网。
- **断言基于契约钩子**：统一用 `[data-test="..."]` 选择器与 `emitted()`，不做整段 DOM 快照，因而对样式改动不敏感。
- **边界覆盖**：空列表、缺 props、NaN、越界音量/进度、超长输入、脏数据条目。
- **契约优先**：新增或修改组件接口前先改 `CONTRACT.md`，再同步 `src/` 与测试。

### 已知偏差（实现 vs 契约）

记录在此以便后续修复，测试中已用注释标注真实行为：

1. `SearchBar.vue`：契约 §2.1 要求 `resultCount` 为 `NaN` 时按 0 处理，实现直接插值，会渲染「显示 NaN / 0 首」。
2. `ProgressBar.vue`：`duration = NaN` 时 `NaN <= 0` 为假，`data-disabled` 保持 `'false'`（非法时长未置灰）；契约要求进度百分比"保留一位小数"，实现输出整数百分比。
3. `VolumeControl.vue`：越界 `volume`（如 1.4 / -0.5）只影响文案（`formatVolume` 会夹紧），range 的 `:value` 未夹紧。

## 离线与音频说明

`src/data/tracks.js` 中每首歌的 `src` 形如 `tone:440:28`，播放时由 `src/utils/tone.js` 在浏览器里即时合成 44.1kHz / 16-bit 单声道 PCM WAV（基频 + 谐波 + 包络）并转成 Blob URL 交给 `<audio>`。想换成真实歌曲，把 `src` 改成 `https://...` 或 `/assets/xxx.mp3` 即可，其余代码无需改动。
