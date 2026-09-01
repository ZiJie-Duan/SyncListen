# SyncListen

**极简语音工作流 —— 崩溃不丢字：会话与录音全程落盘，随时恢复。**

[English](README.md)

---

## 是什么

SyncListen 用最少的按键把你说出口的话变成整理好的文字。为 Sway/Linux 设计，但只要有麦克风、Python 和剪贴板工具，到哪都能跑。

整个 UI 就是一个屏幕、一键一动作、没有菜单：

| 键 | 模式 | 作用 |
|---|---|---|
| `回车` | **写入** | 录音（电平 + 时间轴 HUD）→ 流式转写（边说边出字）→ AI 润色 → 追加 → 自动复制剪贴板 |
| `S` | **升华写入** | 同写入，但 AI 深度润色：口语转书面、精炼有逻辑（仅在线） |
| `E` | **编辑** | 用 `$EDITOR` 打开当前内容编辑 |
| `A` | **AI 指令** | 语音输入指令 → AI 按指令重写当前内容 |
| `M` | **记忆** | 查看/编辑/清空长期记忆（作为识别语境，跨重启持久化） |
| `D` | **清空** | 清空当前内容（可撤销）；文稿快照交给长期记忆归档 |
| `Z` / `X` | 撤销 / 重做 | 在历史栈中前后翻。启动后按一次 `Z` 即回到上次会话的内容 |
| `C` | **复制** | 手动复制当前内容到剪贴板 |
| `Q` | **退出** | 退出。内容已随每次操作落盘，下次启动按 `Z` 找回 |

文稿、历史与原始录音的落盘策略见下文 [崩溃安全](#崩溃安全鲁棒性)。

## 快速上手

```bash
git clone https://github.com/ZiJie-Duan/SyncListen.git ~/SyncListen
cd ~/SyncListen

# 1. 系统包
sudo apt install libportaudio2 xclip wl-clipboard

# 2. Python 依赖（没有 NVIDIA GPU 请先装 CPU 版 torch）
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# 3.（可选）配置 AI Key
echo 'OPENAI_API_KEY=sk-...' > .env

# 4.（可选）编辑模式增强 —— 见下文 "编辑模式增强"
bash scripts/setup-editor-nvim.sh

# 5. 启动
./run.sh
```

首次运行会从 ModelScope 下载 SenseVoiceSmall 语音模型（约 300 MB）到 `~/.cache/modelscope/`，仅一次。

## 崩溃安全（鲁棒性）

三条底线：**程序可以出错，但不退出；崩溃可以，录完的东西不能丢。**

- **文稿与历史即时落盘** — 每次写入 / 撤销 / 重做 / 清空都原子写盘（临时文件 + fsync + 原子替换）。**启动时屏幕是空白的，上次内容留在历史里，按一次 `Z` 就回来**——正常退出、Ctrl+C、崩溃一视同仁。历史默认保留最近 200 个版本（`SESSION_HISTORY_LIMIT`）。落盘失败（磁盘满）不会中断操作：内容留在内存、照常进剪贴板，界面顶部持续警告直到恢复。
- **录音全程写盘** — 录音一开始就由独立线程把每块音频写进磁盘流水账（`.pcm`，无缓冲直写内核，每秒 fsync），停止时原子转成 WAV 存档（`~/.local/state/synclisten/audio/`）。进程崩溃零丢失；断电最多丢最后约 1 秒（`JOURNAL_SYNC_SECONDS`）。录音中途崩溃，下次启动自动把流水账转成 `*.keep.wav` 并在首屏提示路径。
- **失败件受保护** — 转写失败、中断、异常的录音会被改名为 `*.keep.wav`，不参与自动清理；普通录音只保留最近 50 份（`AUDIO_KEEP_COUNT`）。
- **流式转写、边说边出字** — 在线默认走 **Qwen3-ASR-Flash-Realtime**（WebSocket 真流式）：说话时草稿约 0.3 秒就出现在屏幕中间区（弱化显示的 `⋯` 行），停顿后由服务端 VAD 切句定稿；记忆与已有文稿作为 context 注入实时会话。停止录音后只需等最后一句定稿（通常 < 1 秒）。
- **实时断了也不丢** — 实时会话建不起来或中途断开时，自动热切换到**分段转写**（约 5 秒一段，`CHUNK_TARGET_SECONDS`；切点落在窗口末尾最安静的 200 ms 停顿处，能量最低点、无阈值判定），只补转「尚无定稿」的那部分音频，已定稿的句子不丢也不重复；单段云端出错自动改本地转写。`ONLINE_ASR_ENGINE=qwen` 可固定用分段模式。全部转完后才进入 AI 润色。
- **录音 HUD** — 录音时文稿下方是一组绝对刻度（−60 ~ 0 dBFS，录音软件电平表的常规量程，不做自动增益）的实时仪表：电平条带 VU 弹道与峰值保持，条上有 −20 / −6 dBFS 两条刻度线；终端够高时再加一条**录音时间轴**——每格一个时间片的真实 RMS，已被识别定稿覆盖的部分正常墨色、还欠着的尾巴压暗，谷底是停顿、`╹` 是断句落点，一眼看出"转写落后了多少"。麦克风没在采集就是空条 + 平基线。同一行显示时长、实时句数/分段进度、已识别字数，出现丢帧、写盘失败或实时中断会直接告警。
- **出错不退出** — 任何命令抛异常都只显示错误与日志路径（`~/.config/synclisten/logs/`），主界面照常可用。Ctrl+C 在录音中 = 停止录音（已录内容照常转写），在转写收尾中 = 保留已识别部分，在菜单 = 退出；`SIGTERM` / 关终端 = 安全收尾后退出。停录写 WAV 与提交落盘处于临界区，信号不会打断它们。

## 界面与特效

纯 ANSI 转义 + numpy，没有引入任何 UI 框架；宽度按显示列数计算（CJK 按 2 列），20% 宽的 Sway 面板里也不会错位。

**只有明暗，没有色相。** 层次靠灰阶（24 级墨阶）、字重、细线和留白；动效来自物理量而不是彩虹渐变——表针有 VU 弹道，峰值按 PPM 速率回落，高光像打在金属上一样掠过标题。在线/离线用 `●`/`○` 区分，告警用字重 + `▲`，全程不依赖颜色识别信息（对色觉差异友好，也不会在浅色终端里刺眼）。

- **启动画面** — 字距拉开的字标 + 一只在旁边做动作的猫 + 启动日志（上次内容/找回的录音/网络探测），网络探测在后台线程并行，不拖慢启动。
- **录音 HUD** — 呼吸的 `● REC` 计时；电平条带 VU 弹道（IEC 60268-17，300 ms 到 99%）、亚格填充、峰值保持（IEC 60268-18，20 dB / 1.7 s）和 −20 / −6 dBFS 两条绝对刻度线；说话时状态栏显示「说话中」（服务端 VAD 的判定）。
- **录音时间轴（磁带）** — HUD 第二、三行，每格一个时间片（长度 = 断句静音的一半，保证停顿里至少落两格）。柱高是该片真实的 RMS，同一 dBFS 绝对刻度；**已被识别定稿覆盖的部分是正常墨色，还没定稿的尾巴压暗**，两者的分界就是转写落后了多少；谷底是停顿，基线上的 `╹` 是服务端 VAD 的断句落点。它是仪表，不是装饰。
- **打字机上屏** — 识别结果逐字揭示，最后一行带拖影（越新的字越亮）；实时模式下未定稿的草稿以弱化斜体的 `⋯` 行显示，定稿后并入正文。
- **猫** — 参考 oneko（X11 上那只追鼠标的猫）：不是一个图标，而是一只 3 行 × 7 列、有身体有姿态的小动物。会走（两帧换脚、尾巴摆动）、撞到边上先挠两下再转身、停下来坐着、坐久了伸懒腰、一直没动静就趴下睡（冒 `z`），一有动静立刻竖耳朵（`( O.O ) !`）。它出现在启动画面、等待动画、录音跑道和屏保里——全程是同一套行为。
- **录音跑道** — 终端高度够（HUD 预算 ≥ 5 行）时，时间轴下面给猫一条跑道：你开口说话、又出一句字它就竖耳朵走动，安静二十秒它就在那儿睡着。
- **等待动画** — AI 润色 / 升华 / 指令与实时收尾期间：标题掠光 + 计时 + 一条猫跑道，猫在下面自己溜达；AI 调用在后台线程，Ctrl+C 随时可中断（写入原始转写）。
- **写入揭示** — 提交成功时新增文本逐字揭示、提示行掠光两拍。
- **猫之矩阵屏保** — 主界面空闲 `FX_IDLE_SECONDS`（默认 120 秒）后进入：单色数字雨里有一只猫在散步（实心的，雨不会从它肚子里漏出来；走到边上换个高度折返），暗处偶尔亮起一对会眨的眼睛。任意键唤醒（该键不会被当成命令）。
- **关掉它们** — `SYNCLISTEN_FX=0`（或设置 `NO_COLOR`）退化为纯文本界面（刻度线、时间轴、断句标记照常可读）；`FX_IDLE_SECONDS=0` 只关屏保。

## 资源清单

SyncListen 用到的所有外部依赖一表汇总。

| 层级 | 组件 | 来源 | 许可证 | 用途 |
|---|---|---|---|---|
| 运行时 | Python ≥ 3.9 | system | PSF | 语言运行时 |
| 系统 | `libportaudio2` | apt | MIT | `sounddevice` 的 PortAudio 运行时 |
| 系统 | `xclip` | apt | GPL-2 | X11 剪贴板后端 |
| 系统 | `wl-clipboard` | apt | GPL-2 | Wayland 剪贴板后端 |
| Python | `funasr` | PyPI | MIT | SenseVoice 推理框架 |
| Python | `torch`, `torchaudio` | PyPI | BSD-3 | 张量后端（无 GPU 推荐 CPU 版） |
| Python | `numpy` | PyPI | BSD-3 | 音频缓冲数学 |
| Python | `sounddevice` | PyPI | MIT | 麦克风采集 |
| Python | `openai` | PyPI | Apache-2.0 | OpenAI 兼容 API 客户端 |
| Python | `python-dotenv` | PyPI | BSD-3 | 加载 `.env` |
| Python | `modelscope` | PyPI | Apache-2.0 | 模型下载器 |
| 模型 | **SenseVoiceSmall** | ModelScope，首启自动下载 | Apache-2.0 | 多语种语音识别（中/英/粤/日/韩） |
| AI 服务 | OpenAI 兼容聊天 API（默认 DeepSeek `deepseek-chat`） | 各厂商 | 各厂商 | 写入模式润色、A 模式指令 |
| 编辑器（可选） | Neovim ≥ 0.10 | 上游 | Apache-2.0 | `[E]` 编辑模式后端 |
| 编辑器（可选） | `folke/lazy.nvim` | GitHub | Apache-2.0 | 插件管理器（脚本自动安装） |
| 编辑器（可选） | `folke/flash.nvim` | GitHub | Apache-2.0 | 搜索标签跳转 |
| 编辑器（可选） | `mozillazg/pinyin-data` | GitHub | MIT | `etc/nvim/lua/pyinitial_data.lua` 的拼音首字母表数据来源 |

## 持久化路径

SyncListen 的所有落盘工件如下。它们都不含敏感信息，可以整体备份。

| 路径 | 内容 | 删除影响 |
|---|---|---|
| `~/.local/state/synclisten/session.json` | 会话文稿 + 完整历史栈（每次提交/撤销即时落盘） | 回到空白会话；损坏文件会另存 `.corrupt-*` 留证 |
| `~/.local/state/synclisten/audio/` | 每次录音的 WAV；普通件只保留最近 50 份（`AUDIO_KEEP_COUNT`），`*.keep.wav`（失败/中断/崩溃找回件）不自动清理 | 历史录音丢失 |
| `~/.local/state/synclisten/edit/` | 编辑草稿。编辑器异常退出时**不会**被删除；保留最近 20 份 | 仅丢失尚未提交的草稿 |
| `~/.config/synclisten/memory.json` | 长期记忆（`M` 查看，跨重启持久化） | 记忆丢失（清空时自动存 `.bak`） |
| `~/.config/synclisten/logs/` | 异常日志（崩溃/报错现场） | 可删 |
| `~/.config/synclisten/terminology.json` | （旧版遗留）错词表，程序不再读写 | 可删 |
| `~/.cache/modelscope/` | SenseVoiceSmall 模型权重（~300 MB） | 下次启动重下 |
| `~/.config/synclisten-nvim/` | 可选 nvim 配置目录 | 重跑 `scripts/setup-editor-nvim.sh` 即可 |
| `~/.local/share/synclisten-nvim/lazy/` | lazy.nvim 装的插件 | 下次启动 nvim 自动重拉 |
| `~/.local/state/synclisten-nvim/undo/` | 编辑器持久 undo 历史 | 跨会话 undo 不可恢复 |

编辑草稿目录与持久 undo 是 `[E]` 模式的两层兜底：nvim 崩溃时草稿文件**不会被删**，nvim 自己的 undo 文件可以重放每一步改动。

## 编辑模式增强（可选）

默认 `[E]` 走 `$EDITOR`（没设就 fallback 到 `vi`）。本仓库另外提供一份**沙箱化的 nvim 配置**，把 flash.nvim 装进去并加上**中文友好的拼音首字母跳转** —— 不需要学双拼。

> **关于这两个辅助脚本。** `scripts/setup-editor-nvim.sh` 和 `scripts/gen-pyinitial-data.py` 仅在作者的环境（Pop!_OS / Linux、bash、Neovim ≥ 0.10）上验证过。换其他发行版、换 shell、换 Neovim 版本时可能需要微调 —— 包名不同、依赖缺失、路径与 XDG 默认值不一致等。
>
> 如果脚本报错，**不要手工硬调**。脚本本身很短、彼此独立，AI 助手（Claude、ChatGPT 等）改起来很容易。把脚本源码连同完整报错喂给助手，让它"针对我的系统/Shell/Neovim 版本修一下"即可。你不需要去理解拼音码表、vim 内部、lazy.nvim 的引导协议。

### 你能得到什么

- 一份独立 nvim 配置：`~/.config/synclisten-nvim/`，通过 `NVIM_APPNAME` 与日常 `~/.config/nvim/` 完全隔离。
- `flash.nvim` 在 normal/visual/operator-pending 模式下绑到 `s`，配套一个 50 行的小 matcher（`etc/nvim/lua/pyinitial.lua`）：把用户输入的每个 ASCII 字母扩展成"该字母 + 所有以该字母为拼音首字母的汉字"的字符类。所以 `s zw` 能跳到 "中文"、"找位"以及屏幕上每一个 z 起首字 + w 起首字的位置。
- **大写 label**（`A`-`Z`）：与小写拼音输入不冲突，按 `Shift+字母` 跳转。
- 启用持久 undo、关闭 swap —— 草稿可恢复。

### 安装

```bash
# 1. 装 Neovim ≥ 0.10
sudo apt install neovim
# 或装最新版：
curl -LO https://github.com/neovim/neovim/releases/latest/download/nvim-linux-x86_64.appimage
chmod +x nvim-linux-x86_64.appimage && sudo mv nvim-linux-x86_64.appimage /usr/local/bin/nvim

# 2. 装 SyncListen 的 nvim 配置（幂等可重跑）
bash scripts/setup-editor-nvim.sh

# 3. 在 shell 配置里加（~/.bashrc / ~/.zshrc / fish config）
export EDITOR='env NVIM_APPNAME=synclisten-nvim nvim'

# 4. 重新 source 后，跑 SyncListen，按 E
```

### 用法

| 输入 | 效果 |
|---|---|
| `s` | 触发 flash.nvim 搜索 |
| `s z` | 高亮屏幕上所有以 `z` 为拼音首字母的汉字（中、找、在、自……）以及字面 `z`/`Z` |
| `s zw` | 高亮"z 起首字 + w 起首字"的串（中文、找位、自我……） |
| `Shift+<label>` | 跳到带 label 的候选位置（label 用大写避免与小写拼音输入冲突） |
| `v` 再 `s zw` | 同上但选区延伸 —— 即"选中文词"的标准操作 |
| `s abc` | 退化为字面 ASCII 匹配，英文/代码场景照常工作 |
| `<leader>w`（即 `空格 w`） | `:wq` —— 保存并返回 SyncListen |

### 文件结构

```
etc/nvim/
├── init.lua                  # nvim 配置入口；引导 lazy.nvim、设 undo/swap、加载 flash
└── lua/
    ├── pyinitial.lua         # flash search.mode 钩子
    └── pyinitial_data.lua    # 内嵌的汉字→拼音首字母表（约 77 KB，含 U+4E00–U+9FFF 共 25721 字）
```

`pyinitial_data.lua` 由 `mozillazg/pinyin-data` 生成。终端用户**不需要**重新生成。维护者可以这样刷新：

```bash
python3 scripts/gen-pyinitial-data.py
# 或基于本地的 pinyin.txt：
python3 scripts/gen-pyinitial-data.py --source /path/to/pinyin.txt
```

## Sway 集成（可选）

```text
# ~/.config/sway/config
bindsym $mod+p exec ~/bin/sway-synclisten
```

```bash
# ~/bin/sway-synclisten
#!/bin/bash
if swaymsg -t get_tree | jq -e '.. | .app_id? == "synclisten"' | grep -q true; then
    swaymsg '[app_id="synclisten"] focus'
    exit 0
fi
swaymsg splith
swaymsg exec 'foot --app-id=synclisten -e /bin/bash -ic "cd ~/SyncListen && ./run.sh; bash"'
sleep 0.5
swaymsg resize set width 20 ppt
```

```bash
chmod +x ~/bin/sway-synclisten
```

按 `Super+P` 打开（或聚焦）一个 20% 宽度的 foot 终端跑 SyncListen。

## 环境变量速查

| 变量 | 是否必填 | 默认值 | 作用 |
|---|---|---|---|
| `OPENAI_API_KEY` | 可选 | — | 不填则 `[S]` `[A]` 不可用，写入模式回退为原始转写 |
| `OPENAI_BASE_URL` | 可选 | `https://api.deepseek.com/v1` | 任意 OpenAI 兼容服务地址 |
| `AI_MODEL` | 可选 | `deepseek-chat` | 传给 AI 的 model 名 |
| `DASHSCOPE_API_KEY` | 可选 | — | 阿里云百炼 Key，启用在线云端转写（Qwen3-ASR-Flash-Realtime 流式，失败回退分段）；不填则始终本地转写 |
| `ONLINE_ASR_ENGINE` | 可选 | `qwen-realtime` | 在线引擎：`qwen-realtime`（真流式+上下文注入，失败热切换分段）、`qwen`（固定分段+上下文注入）或 `paraformer`（流式，无上下文） |
| `CHUNK_TARGET_SECONDS` | 可选 | `5` | 分段转写（`qwen` 引擎 / 离线 / 实时兜底）的目标段长（秒），实际切点自动落在语音停顿处 |
| `QWEN_ASR_REALTIME_MODEL` | 可选 | `qwen3-asr-flash-realtime` | 实时识别模型名 |
| `DASHSCOPE_REALTIME_URL` | 可选 | 北京地域 wss 地址 | 实时识别 WebSocket 地址（新加坡地域改为 `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime`） |
| `QWEN_ASR_LANGUAGE` | 可选 | 空（自动） | 实时识别语种：空 = 自动检测（中英混读用它）；可设 `zh` / `en` / `yue` / `ja` … |
| `REALTIME_VAD_SILENCE_MS` | 可选 | `400` | 服务端 VAD 断句静音时长（官方推荐 400，范围 200~6000） |
| `REALTIME_VAD_THRESHOLD` | 可选 | `0.0` | 服务端 VAD 灵敏度阈值（官方推荐 0.0，范围 −1~1） |
| `SYNCLISTEN_FX` | 可选 | `1` | 设为 `0` 关闭墨阶与动画（`NO_COLOR` 非空同效） |
| `FX_IDLE_SECONDS` | 可选 | `120` | 主界面空闲多少秒进入猫之矩阵屏保；`0` 关闭 |
| `AUDIO_KEEP_COUNT` | 可选 | `50` | 普通录音 WAV 留存数量，超出启动时从旧到新清理（`0` = 全部保留；`*.keep.wav` 不清理） |
| `JOURNAL_SYNC_SECONDS` | 可选 | `1` | 录音流水账 fsync 间隔（秒）：断电时最多丢这么长的音频 |
| `SESSION_HISTORY_LIMIT` | 可选 | `200` | 会话历史（撤销）保留的版本数上限 |
| `SESSION_FILE` | 可选 | `~/.local/state/synclisten/session.json` | 会话快照路径 |
| `AUDIO_DIR` | 可选 | `~/.local/state/synclisten/audio` | 录音 WAV 存放目录 |
| `MEMORY_FILE` | 可选 | `~/.config/synclisten/memory.json` | 长期记忆路径 |
| `EDITOR` | 可选 | `vi` | `[E]` 模式调用的编辑器；推荐设 `env NVIM_APPNAME=synclisten-nvim nvim` |
| `VISUAL` | 可选 | — | `EDITOR` 未设时的备选 |
| `XDG_STATE_HOME` | 可选 | `~/.local/state` | 会话快照 / 录音 / 编辑草稿的根目录 |
| `MODELSCOPE_CACHE` | 可选 | `~/.cache/modelscope` | SenseVoice 模型缓存目录 |
| `WAYLAND_DISPLAY` / `DISPLAY` | 自动 | — | 运行时探测，决定用 `wl-copy` 还是 `xclip` |

仓库根目录的 `.env` 会被 `python-dotenv` 自动加载，是放 `OPENAI_*` 和 `AI_MODEL` 的推荐位置。

## 常见问题

**没有 AI Key 能用吗？** 能。写入模式直接输出原始转写文字；`S` 和 `A` 不可用。其他功能（录音、转写、编辑、撤销）照常。

**程序崩了 / 断电了，内容丢了吗？** 没丢。文稿与历史每次操作都即时落盘；下次启动屏幕是空白的，按一次 `Z` 就回到崩溃前的内容。录音音频也全程写盘，启动时会自动找回上次未完成的录音、转为 `*.keep.wav`，路径显示在首屏提示区。详见[崩溃安全](#崩溃安全鲁棒性)。

**想关掉动效？** `SYNCLISTEN_FX=0 ./run.sh`（或设置 `NO_COLOR`），界面退化为纯文本；只想关屏保就 `FX_IDLE_SECONDS=0`。

**实时转写会不会偶尔断？** 会有网络抖动的情况，但不影响结果：断开的瞬间自动切到分段转写补齐剩余部分，界面上会出现 `⚠️ 实时→分段`，写入完成后的提示会说明；这次录音的 WAV 会标记为 `*.keep.wav` 保留。

**模型下载太慢/失败？** ModelScope 默认走国内镜像。可以设 `MODELSCOPE_CACHE` 指向新目录重试，或先用 `modelscope` 命令行工具预下载。

**能换非 DeepSeek 后端吗？** 任何 OpenAI 兼容服务都行，改 `.env` 里的 `OPENAI_BASE_URL` 和 `AI_MODEL` 即可。

**编辑时 nvim 崩了，工作丢了吗？** 没丢。`~/.local/state/synclisten/edit/edit-<时间戳>.md` 在异常退出时**不会被删除**，状态栏会回显路径。任何编辑器都可以打开它；持久 undo 历史在 `~/.local/state/synclisten-nvim/undo/`。

**能在我自己的日常 nvim 里用拼音 matcher 吗？** 可以。把 `etc/nvim/lua/pyinitial.lua` 和 `etc/nvim/lua/pyinitial_data.lua` 放到你的 runtime path，再让 `flash.jump` 的 `search.mode` 指向 `require("pyinitial").mode` 即可。`init.lua` 就是完整示例。

## 致谢

本项目站在以下肩膀上：

- [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) 和 [funasr](https://github.com/modelscope/FunASR) —— 本地 ASR 流水线。
- [DeepSeek](https://www.deepseek.com/) —— 平价的 OpenAI 兼容聊天 API。
- [folke/lazy.nvim](https://github.com/folke/lazy.nvim) 与 [folke/flash.nvim](https://github.com/folke/flash.nvim) —— 编辑模式增强。
- [mozillazg/pinyin-data](https://github.com/mozillazg/pinyin-data) —— `pyinitial_data.lua` 背后的拼音数据。

## 许可

见 [LICENSE](LICENSE)。
