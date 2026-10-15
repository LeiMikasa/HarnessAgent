# 接口契约（CONTRACT）—— LZX 音乐台 · Vue 版

> 本文件是**唯一事实来源**。所有组件必须严格按下面的 props / emits / DOM 钩子实现，
> 因为 `src/App.vue` 与测试都依赖这些名字。不要重命名、不要「顺手优化」接口。
> 需要新增接口时，先在提交说明里写清楚，由主 Agent 统一修改契约。

## 0. 项目结构

```
vue-music/
├── index.html
├── vite.config.js          # 含 vitest 配置（jsdom + tests/setup.js）
├── src/
│   ├── main.js
│   ├── App.vue                       # 【主 Agent】页面装配 + 键盘快捷键
│   ├── data/tracks.js                # 【主 Agent】内置歌单（8 首）
│   ├── styles/main.css               # 【主 Agent】全局令牌（CSS 变量）
│   ├── composables/usePlayer.js      # 【主 Agent】播放器核心状态机
│   ├── utils/{format,playlist,tone,audio}.js  # 【主 Agent】纯逻辑/音频引擎
│   └── components/                   # 【组件负责人】纯展示组件
│       ├── SearchBar.vue             # 【组件负责人 A】
│       ├── TrackList.vue             # 【组件负责人 A】
│       ├── ProgressBar.vue           # 【组件负责人 B】
│       ├── VolumeControl.vue         # 【组件负责人 B】
│       └── PlayerBar.vue             # 【组件负责人 B】
└── tests/
    ├── setup.js
    ├── helpers/fake-audio.js
    ├── *.test.js                     # 各自负责的测试文件
```

## 1. 组件总原则

1. **纯展示组件**：组件内**禁止** import `usePlayer` / 访问 `localStorage` / 直接 new Audio。
   一切数据靠 props 传入，一切交互靠 emits 抛出，全部由 `App.vue` 转发给 player。
2. 使用 `<script setup>` + `defineProps` / `defineEmits`；样式写 `<style scoped>`。
3. 颜色、圆角只用 `src/styles/main.css` 里的 CSS 变量（`var(--accent)` 等），
   不要硬编码新的主题色。
4. 每个交互元素都要带 `data-test` 钩子，名称见下（**测试按这些选择器写**）。
5. 无障碍：按钮要有 `aria-label`；进度/音量用 `<input type="range">` + `aria-label`。
6. 组件必须能在没有 `<audio>`、没有真实播放的状态下渲染（props 缺失时给出合理默认值）。

## 2. 组件接口

### 2.1 SearchBar.vue（A）

| 项 | 内容 |
| --- | --- |
| props | `modelValue: String`（默认 `''`）、`resultCount: Number`（默认 0）、`totalCount: Number`（默认 0） |
| emits | `update:modelValue`（输入时，参数为字符串）、`clear`（点击清除） |
| 暴露 | `defineExpose({ focus })`，`focus()` 让输入框获得焦点（App.vue 的 `/` 快捷键用） |

DOM 钩子：
- 根元素 `[data-test="search-bar"]`
- `<input data-test="search-input">`，`v-model` 语义 = emit `update:modelValue`
- 清除按钮 `[data-test="search-clear"]`，仅当 `modelValue` 非空时渲染，点击后 emit `clear`
- 计数 `[data-test="search-count"]`，文案 `显示 {resultCount} / {totalCount} 首`；
  `resultCount === NaN` 时按 0 处理；当 `resultCount < totalCount` 时给根元素加 `data-filtered="true"`

### 2.2 TrackList.vue（A）

| 项 | 内容 |
| --- | --- |
| props | `tracks: Array`（默认 `[]`）、`currentId: String\|null`、`isPlaying: Boolean`、`favorites: Array<String>`（默认 `[]`）、`outline: Array<String>` 无需实现 |
| emits | `select`（参数：track id）、`toggle-favorite`（参数：track id） |

DOM 钩子：
- 根 `[data-test="track-list"]`，空列表时内部渲染 `[data-test="track-list-empty"]`（文案「没有匹配的歌曲」）
- 每个条目 `[data-test="track-item"]`，带 `data-track-id="<id>"`；当前曲目加 `data-active="true"`（否则 `false`）
- 条目内的点击区域 `.track-main` 点击 → emit `select(id)`
- 标题 `[data-test="track-title"]`、歌手 `[data-test="track-artist"]`、时长 `[data-test="track-duration"]`
  （时长用 `formatTime(track.duration)`，即 `src/utils/format.js`；`format.js` 由主 Agent 维护，可 import）
- 收藏按钮 `[data-test="favorite-btn"]`，带 `data-track-id`，收藏中时 `data-active="true"`，
  且 `aria-pressed` 与之一致；点击调用 `event.stopPropagation()` 后 emit `toggle-favorite(id)`
- 封面块 `[data-test="track-cover"]` 显示 `track.cover`，背景使用 `track.color`（没有则用 `var(--panel)`）
- 当前曲目正在播放时，条目加 `data-playing="true"`

### 2.3 ProgressBar.vue（B）

| 项 | 内容 |
| --- | --- |
| props | `currentTime: Number`（默认 0）、`duration: Number`（默认 0）、`disabled: Boolean`（默认 false） |
| emits | `seek`（参数：目标秒数 Number） |

DOM 钩子：
- 根 `[data-test="progress-bar"]`，`duration <= 0` 时加 `data-disabled="true"`
- `<input type="range" data-test="progress-range" min="0" max="100" step="0.1">`
  value = 已播百分比（保留一位小数，`duration<=0` 时为 0）；`input` 事件 → `emit('seek', ratio/100*duration)`
- 填充条 `[data-test="progress-fill"]`，`style.width = 百分比 + '%'`
- 当前时间 `[data-test="progress-current"]` = `formatTime(currentTime)`
- 总时长 `[data-test="progress-duration"]` = `formatTime(duration)`

### 2.4 VolumeControl.vue（B）

| 项 | 内容 |
| --- | --- |
| props | `volume: Number`（0~1，默认 1）、`muted: Boolean`（默认 false） |
| emits | `update:volume`（参数：0~1 的音量）、`toggle-mute`（无参数） |

DOM 钩子：
- 根 `[data-test="volume-control"]`，`data-muted="true|false"`
- `<input type="range" data-test="volume-range" min="0" max="100" step="1">`，value = `round(volume*100)`；
  `input` 事件 → `emit('update:volume', Number(value)/100)`
- 静音按钮 `[data-test="volume-mute"]`，`aria-pressed` = muted，点击 → emit `toggle-mute`
- 音量文案 `[data-test="volume-value"]`：静音时显示「静音」，否则 `formatVolume(volume)`（如 `80%`）

### 2.5 PlayerBar.vue（B）

| 项 | 内容 |
| --- | --- |
| props | `track: Object\|null`、`isPlaying: Boolean`、`currentTime: Number`、`duration: Number`、`volume: Number`、`muted: Boolean`、`shuffle: Boolean`、`repeat: String`（`off`/`all`/`one`） |
| emits | `toggle-play`、`prev`、`next`、`seek`（秒）、`volume`（0~1）、`toggle-mute`、`toggle-shuffle`、`cycle-repeat` |

DOM 钩子：
- 根 `[data-test="player-bar"]`
- 播放/暂停按钮 `[data-test="play-toggle"]`，`aria-label` 在播放时为「暂停」、否则「播放」，
  内部图标文案：播放显示 `▶`，暂停显示 `❚❚`
- 上一首 `[data-test="prev"]`、下一首 `[data-test="next"]`
- 洗牌按钮 `[data-test="shuffle"]`，`aria-pressed` = shuffle
- 循环按钮 `[data-test="repeat"]`，`data-repeat="off|all|one"`，`aria-label` 依次为「顺序播放」「列表循环」「单曲循环」
- 当前曲目标题 `[data-test="now-playing-title"]`（无曲目时显示「未选择歌曲」）、
  歌手 `[data-test="now-playing-artist"]`
- PlayerBar 内部使用 `<ProgressBar>` 与 `<VolumeControl>`，把 props 透传下去，
  并把子组件的 `seek` → emit `seek`、`update:volume` → emit `volume`、`toggle-mute` → emit `toggle-mute`

## 3. 播放器核心 API（只读参考，A/B 一般用不到）

`src/composables/usePlayer.js` → `createPlayer({ tracks, audio, storage, random })` 返回：

```js
player.state          // reactive：tracks, currentIndex, isPlaying, currentTime, duration,
                      //           volume, muted, shuffle, repeat, favorites, query,
                      //           favoritesOnly, error
player.currentTrack   // Track | null
player.visibleTracks  // 搜索/收藏过滤后的列表
player.progress       // 0~1
player.play() / pause() / togglePlay()
player.selectTrack(id) / next() / prev()
player.seek(seconds) / seekRatio(ratio)
player.setVolume(v) / toggleMute()
player.toggleShuffle() / cycleRepeat()
player.setQuery(q) / clearQuery() / toggleFavoritesOnly()
player.toggleFavorite(id) / isFavorite(id)
```

## 4. 测试约定

- 统一放在 `vue-music/tests/`，文件 `*.test.js`，用 `vitest` 的 `describe/it/expect`。
- 运行：`npm test`（= `vitest run`）；只跑自己的文件：`npx vitest run tests/xxx.test.js`。
- 挂载组件：`import { mount } from '@vue/test-utils'`。
- 组件测试**不要**依赖真实播放：需要音频时用 `tests/helpers/fake-audio.js`
  ```js
  import { createFakeAudio } from './helpers/fake-audio.js'
  import { createPlayer, createSafeStorage } from '@/composables/usePlayer.js'

  const audio = createFakeAudio()
  const player = createPlayer({ tracks, audio, storage: createSafeStorage(), autoplayOnSelect: true })
  ```
- 断言用 DOM 钩子：`wrapper.get('[data-test="play-toggle"]')`。
- 每个测试文件至少覆盖：正常路径 + 边界（空数据 / 缺 props / 极值）。
- 每个文件 `afterEach` 里 `wrapper.unmount()`（避免监听器泄漏）。
