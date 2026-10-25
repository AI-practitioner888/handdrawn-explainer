# 本地定制记录（handdrawn-explainer）

本技能由 clean-workspace 从 skillhub `@user_2b46b0cf/handdrawn` v1.0.0 安装后改造，现为本机自有资产，**不再跟踪上游**。

## 与原版的两处差异（已落地）

### 1. 右倾握笔（右手书写姿态）
文件：`assets/renderer/renderer.js` 的三个工具函数

| 工具 | 原值（左倾/左手姿） | 现值（右倾/右手姿） |
|---|---|---|
| 铅笔 pencil | `ctx.rotate(-.70 + …)` | `ctx.rotate(.70 + …)` |
| 橡皮 eraser | `ctx.rotate(-.15 + …)` | `ctx.rotate(.15 + …)` |
| 钢笔/马克笔 inkPen/marker | `ctx.rotate(-.62 + …)` | `ctx.rotate(.62 + …)` |

原理：画布 y 轴向下，正角度=顺时针，笔杆顶端偏到笔尖右侧=右手握笔自然姿态。手抖微扰（`sin(time*8)*.035` 等）保留。

### 2. 真实中文笔顺
- 原版：`scripts/compile_assets.py` 用 Zhang-Suen 骨架细化 + 空间排序，得到"近似空间顺序，不是真实笔顺"（原注释原话）。
- 改造：`assets/stroke-order/medians.json`（6.5MB，9574 字，来源 Make Me a Hanzi，基于文鼎楷体 Arphic 许可，与霞鹜文楷同源）提供每个字的真实笔顺折线（1024 坐标、y 向下）。
- 新函数 `load_medians()` + `medians_to_strokes()`：把 medians 折线按 atlas 字形实际包围盒线性映射对齐，生成 `strokes`。命中 medians → 真实笔顺；未命中（生僻字/符号）→ 回退原骨架法。
- **`renderer.js` 未改动**，它只读 `strokes` 数组顺序逐笔显现，换来源即生效。
- 验证：`十` 先横后竖；`你`（7画）先撇后竖再尔部；笔画数全部对上真实笔顺（中4/一1/十2/好6/世5/界9/你7）。

## 自带资源（skillhub 原包缺失，已补齐）
- `assets/fonts/LXGWWenKai-Regular.ttf`（霞鹜文楷 v1.501，SIL OFL，18.2MB）—— 官方 GitHub 下载，构建流程硬性依赖。
- `LICENSES/LXGWWenKai-OFL.txt` —— 字体许可证。
- `assets/stroke-order/medians.json` + `assets/stroke-order/SOURCE-LICENSE.txt` —— 笔顺数据及 Arphic 许可说明。

## 运行环境（已验证，优先复用，零付费）
- Python：托管 venv `C:\Users\12144\.workbuddy\binaries\python\envs\default`（numpy/Pillow/opencv-headless/playwright/edge-tts 已齐）。
- FFmpeg/ffprobe：WinGet 完整版在位。
- 浏览器：复用微软 Edge（Chromium），通过 `HANDDRAWN_BROWSER` 指定，**不下载** Playwright Chromium。
- 中文配音：`edge-tts` 默认 `zh-CN-XiaoxiaoNeural`（免费）。
- doctor：`python doctor.py`（设 `HANDDRAWN_BROWSER` 指向 Edge）全绿。

## 调用方式
- WorkBuddy 技能目录里的 `handdrawn-explainer` 是 junction，指向本真源。
- 构建命令示例（用绝对路径调本真源，避免依赖 junction 名）：
  ```
  EDGE=$(cygpath -w "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
  HANDDRAWN_BROWSER="$EDGE" \
  /c/Users/12144/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
  D:/Workspace/clean-workspace/skills/handdrawn-explainer/scripts/build.py \
  --project <工程>/project.json --output <工程>/build \
  --audio-plan <工程>/narration.json --render
  ```
- 旁白连续性：`continuous` 策略要求静音间隔 <500ms 才过闸门；测试/刻意停顿用 `intentional` 策略绕过。正式片建议调好节奏用 `continuous`。

## 中文配音多音字处理（2026-10-25 实测；探针机制 2026-10-25 并入「陪你夜读」团队成果）

**结论：Edge 免费 TTS 端点不接受 `<phoneme>`，无法强制注音。**
实测四种 alphabet（`sapi` / `pinyin` / `ups` / `ipa`）全部返回 `NoAudioReceived`；
纯文本 SSML 可正常合成，说明是 phoneme 标签被服务端拒收，不是 SSML 通道问题。
（若要试：patch `edge_tts.communicate.mkssml(tc, escaped_text)`，遇到 `&lt;speak`/`&lt;phoneme` 时
`unescape` 后原样返回即可绕过 edge-tts 的转义——但这只能证明通道通，读音仍改不了。）

因此只有三条可行手段，按优先级：

1. **等义词规避（首选，零成本）**——把多音字换成唯一读音的同义词，画面文字不受影响：
   | 原词 | 误读 | 改后 |
   |---|---|---|
   | 这一行 | xíng（应 háng） | 这一排 / 这一列 |
   | 当一个整体 | dāng（应 dàng） | 作为一个整体 |
   | 传不了 | le（应 liǎo） | 没法传 |
   | 都是 | dū（应 dōu） | 均为 |
2. **同音字诱导**——`audio.py` 原生支持 `spokenText` 与 `text` 分离：
   `text` 写正确汉字（用于字幕与工程显示），`spokenText` 写同音字强制读音
   （如 `text="测量"` / `spokenText="测良"`）。仅在规避会损伤语义时使用。
3. **客观核验读音**——转写（ASR）分不出同形字的读音，用声学比对：
   ```
   python scripts/polyphone_check.py 测量 测良 测亮
   ```
   合成待测词与各读音的诱导参照词，算 log-mel 谱 DTW 距离，最近者即实际读音。
   实测「测量」DTW(测良)=0.062 vs DTW(测亮)=1.679 → 判为 cè liáng，正确。
   脚本无 scipy/librosa 依赖（手写 mel 滤波与 DTW），已随技能分发。

**重录必踩的坑**：`audio.py` 会把输出目录里的 `plan.json` 改写成 portable 版（每个 segment 带
`audioFile`），而它遵循 "Existing audioFile wins over TTS" —— 直接改文本再跑同一份 plan，
**旧 wav 会被原样复用，音频根本没重录**。必须另写一份不含 `audioFile`、带
`mode="edge"` + `trimSilence=true` 的纯 TTS plan 来重录（输出文件名 `序号-id.wav` 相同，会覆盖）。
重录后重新测量时长并回写 `dur.json`，再重跑排期生成器。

### 探针机制（2026-10-25 从「陪你夜读」团队并入，互补整合）

夜读专家团在 `outputs\陪你夜读-日更流水线\` 沉淀了三层防多音字体系，本技能吸收其两层、
保留自研一层：

1. **稿子侧定音（并入）**——判据：每个多音字在所在句里读音必须唯一，不需要听者判断。
   歧义消除不了就改稿（换等义词/改结构/拆行），不留给配音阶段。
2. **全稿扫查清单（并入）**——`polyphone_check.py --scan narration.json`：
   逐处输出【上下文】+ 两音 + 典型搭配 + 探针字，供人工定「本处应读音」。
3. **录音后客观核验（自研保留）**——log-mel DTW 整词比对，比夜读版的
   FFT 相关+比例估窗更稳（不依赖时间窗估算）。探针字放进同款句式当参照词即可。

**单一真源** `scripts/polyphone_probes.json`（49 字种）：探针表+语境搭配表，
script_lint.py 的 A2 风险字表与 polyphone_check.py 共用此表。
来源=夜读团队 polyphone_probe.py 30 字探针表 + 审稿关卡3 盯防表，
并入白板线实测字（传/调/缝/假/省/兴/屏/相/卡/系/教）成超集；
**已修正原表 3 处探针字不同音的 bug**：当 dāng 刀(dāo)→裆、弹 tán 单(dān)→谈、重 zhòng 中(zhōng)→众。

```
python scripts/polyphone_check.py --scan narration.json     # 全稿逐处清单（人工定音）
python scripts/polyphone_check.py 测量 测良 测亮             # 单词 DTW 客观核验
```

## 开工红灯门禁 script_lint.py（2026-10-25 移植自花叔 storyboard_lint 机制）

动手 build 之前跑机械门禁，**红灯不放行、每条带修法**（退出码：红灯 1 / 黄或绿 0 / 读不了 2）：

```
python scripts/script_lint.py narration.json project.json   # 顺序无所谓，按内容自动识别
python scripts/script_lint.py narration.json                # 只有配音时单查 Stage A
```

覆盖的检查 = 本机踩过的坑全集合：
- **A1 句长**（>8s 红灯：v2 声画漂移根因）｜**A2 多音字风险字**（黄灯，配合 polyphone_check）｜**A3 开场抽象定义**（红灯，concept-explain-writer 反模式）｜**A4 术语负债**（一板 >2 个黄灯）｜**A5 停顿断档**
- **B1 绑定完整性**（drawGroup = "g"+段id 约定）｜**B2 同步判据**（领先 >320ms / 拖拍 >220ms 红灯，可 `--lead-tol/--finish-tol` 调）｜**B3 长句多元素**｜**B4 drawGroup 跨组夹层**（整板压缩的根因，红灯）｜**B5 帧边界**｜**B6 收尾留白**（<300ms 黄、画出场景边界红）｜**B7 首元素 startMs=0**

实测：v3 成片数据 0 红 16 黄通过（黄灯均为已人工核验项）；人工埋 4 类坑全部命中。
**实现坑（改代码注意）**：场景 id 形如 `board3`，数字不在开头——提取板号必须用 `re.search` 而非 `re.match`（match 锚定开头返回 None，会静默跳过全部 B 类检查，且不报错）。
