# SyncListen

**极简语音工作流 — 一次运行，一个会话，退出不保存。**

[English](#english) | [中文](#中文)

---

## 中文

### 是什么

SyncListen 是一个为 Sway/Linux 设计的极简语音速记工具。核心理念：**最快路径把你说的话变成整理好的文字**。

- 按 **回车** → 录音 → 自动转写润色 → 内容追加 → 自动复制剪贴板
- 按 **A** → 录音说指令 → AI 按指令处理当前内容
- 按 **Z/X** → 撤销 / 重做
- 按 **D** → 清空当前内容
- 按 **C** → 手动复制到剪贴板
- 按 **Q** → 退出

没有文档管理、没有持久化、没有复杂菜单。打开就用，用完就走。

### 环境要求

- Python 3.9+
- Linux (Wayland 或 X11)
- 麦克风

### 安装

**1. 克隆仓库**

```bash
git clone https://github.com/ZiJie-Duan/SyncListen.git ~/SyncListen
cd ~/SyncListen
```

**2. 安装系统依赖**

```bash
# Ubuntu / Debian / Pop!_OS
sudo apt install libportaudio2 xclip wl-clipboard
```

| 包 | 用途 |
|---|---|
| `libportaudio2` | 麦克风录音运行时库 |
| `xclip` | X11 剪贴板 |
| `wl-clipboard` | Wayland 剪贴板 |

**3. 安装 Python 依赖**

```bash
pip install -r requirements.txt
```

> **注意**：默认 `torch` 是 CUDA 版（非常大）。如果你的机器没有 NVIDIA GPU，建议安装 CPU 版以节省时间和空间：
> ```bash
> pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
> pip install -r requirements.txt
> ```

**4. 配置 AI API（可选）**

没有 API Key 也能使用，只是 **A 模式（AI 指令）不可用**，回车模式的润色功能也会回退到原始转写。

在项目根目录创建 `.env`：

```bash
OPENAI_API_KEY=sk-your-key-here
# 以下可选，不填则使用 DeepSeek 默认值
# OPENAI_BASE_URL=https://api.deepseek.com/v1
# AI_MODEL=deepseek-chat
```

### 启动

```bash
cd ~/SyncListen
./run.sh
```

或直接用 Python：

```bash
python main.py
```

第一次启动时，会自动从 ModelScope 下载 SenseVoiceSmall 模型（约 300MB），只需一次。模型缓存位于 `~/.cache/modelscope/`。

### Sway 集成（可选）

将以下加入 `~/.config/sway/config`：

```
bindsym $mod+p exec ~/bin/sway-synclisten
```

创建启动脚本 `~/bin/sway-synclisten`：

```bash
#!/bin/bash

# 已存在？直接聚焦
if swaymsg -t get_tree | jq -e '.. | .app_id? == "synclisten"' | grep -q true; then
    swaymsg '[app_id="synclisten"] focus'
    exit 0
fi

swaymsg splith
swaymsg exec 'foot --app-id=synclisten -e /bin/bash -ic "cd ~/SyncListen \&\& ./run.sh; bash"'
sleep 0.5
swaymsg resize set width 20 ppt
```

```bash
chmod +x ~/bin/sway-synclisten
```

按 **Super+P** 打开侧栏窗口（已存在则聚焦），SynListen 会在右侧以 20% 宽度启动。

### 使用场景

| 场景 | 操作 |
|------|------|
| 快速记想法 | 回车 → 说话 → 回车停止 |
| 整理已有内容 | A → "总结成三点" → 回车 |
| 写错了 | Z 撤销 |
| 要清屏 | D 清空（可撤销） |
| 粘贴到别处 | C 复制（每次写入后已自动复制） |

### 项目结构

```
SyncListen/
├── main.py                  # 入口
├── run.sh                   # 启动脚本
├── requirements.txt         # Python 依赖
├── synclisten/
│   ├── config.py            # 全局配置
│   └── core/
│       ├── recorder.py      # 音频录制
│       ├── transcriber.py   # SenseVoice 转写
│       └── ai_client.py     # AI API 客户端
└── .env                     # API Key（不提交到 git）
```

### 常见问题

**Q: 没有 API Key 能用吗？**
A: 能。回车模式会跳过 AI 润色，直接输出原始转写文字。只有 A 模式（AI 指令）完全不可用。

**Q: 模型下载太慢/失败？**
A: ModelScope 默认从国内镜像下载。如果仍然慢，可以设置环境变量 `export MODELSCOPE_CACHE=~/.cache/modelscope` 指定缓存路径，或检查网络连接。

**Q: 可以用其他 AI 服务吗？**
A: 可以。任何兼容 OpenAI API 格式的服务都可以，修改 `.env` 中的 `OPENAI_BASE_URL` 和 `AI_MODEL` 即可。

---

## English

### What is it

SyncListen is a minimalist voice-to-text workflow tool for Sway/Linux. Core idea: **fastest path from speech to polished text**.

- Press **Enter** → record → transcribe → AI polish → append → auto-copy to clipboard
- Press **A** → speak an instruction → AI processes current content
- Press **Z/X** → undo / redo
- Press **D** → clear content (undoable)
- Press **C** → copy to clipboard
- Press **Q** → quit

No document management, no persistence, no complex menus. Open, use, close.

### Requirements

- Python 3.9+
- Linux (Wayland or X11)
- Microphone

### Installation

**1. Clone**

```bash
git clone https://github.com/ZiJie-Duan/SyncListen.git ~/SyncListen
cd ~/SyncListen
```

**2. System dependencies**

```bash
# Ubuntu / Debian / Pop!_OS
sudo apt install libportaudio2 xclip wl-clipboard
```

| Package | Purpose |
|---------|---------|
| `libportaudio2` | Microphone recording runtime |
| `xclip` | X11 clipboard |
| `wl-clipboard` | Wayland clipboard |

**3. Python dependencies**

```bash
pip install -r requirements.txt
```

> **Note**: The default `torch` from PyPI is the CUDA version (very large). If you don't have an NVIDIA GPU, install the CPU version to save time and disk space:
> ```bash
> pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
> pip install -r requirements.txt
> ```

**4. Configure AI API (optional)**

You can use SyncListen without an API Key — the **A mode (AI instruction)** will be unavailable, and Enter mode will fall back to raw transcription without AI polish.

Create `.env` in the project root:

```bash
OPENAI_API_KEY=sk-your-key-here
# Optional — defaults to DeepSeek if omitted
# OPENAI_BASE_URL=https://api.deepseek.com/v1
# AI_MODEL=deepseek-chat
```

### Launch

```bash
cd ~/SyncListen
./run.sh
```

Or directly with Python:

```bash
python main.py
```

On first launch, the SenseVoiceSmall model (~300MB) will be auto-downloaded from ModelScope. One time only. Model cache is at `~/.cache/modelscope/`.

### Sway Integration (optional)

Add to `~/.config/sway/config`:

```
bindsym $mod+p exec ~/bin/sway-synclisten
```

Create the launcher script `~/bin/sway-synclisten`:

```bash
#!/bin/bash

# Already open? Just focus
if swaymsg -t get_tree | jq -e '.. | .app_id? == "synclisten"' | grep -q true; then
    swaymsg '[app_id="synclisten"] focus'
    exit 0
fi

swaymsg splith
swaymsg exec 'foot --app-id=synclisten -e /bin/bash -ic "cd ~/SyncListen \&\& ./run.sh; bash"'
sleep 0.5
swaymsg resize set width 20 ppt
```

```bash
chmod +x ~/bin/sway-synclisten
```

Press **Super+P** to open the side panel (or focus if already open). SyncListen starts on the right at 20% width.

### Use Cases

| Scenario | Action |
|----------|--------|
| Quick note | Enter → speak → Enter to stop |
| Process existing content | A → "summarize in 3 points" → Enter |
| Made a mistake | Z to undo |
| Clear the screen | D to clear (undoable) |
| Paste elsewhere | C to copy (already auto-copied on every write) |

### Project Structure

```
SyncListen/
├── main.py                  # Entry point
├── run.sh                   # Launcher script
├── requirements.txt         # Python dependencies
├── synclisten/
│   ├── config.py            # Global config
│   └── core/
│       ├── recorder.py      # Audio recording
│       ├── transcriber.py   # SenseVoice transcription
│       └── ai_client.py     # AI API client
└── .env                     # API Key (do not commit)
```

### FAQ

**Q: Can I use it without an API Key?**
A: Yes. Enter mode will output raw transcription without AI polish. A mode (AI instruction) will be unavailable.

**Q: Model download is slow / fails?**
A: ModelScope downloads from its default mirror. You can set `export MODELSCOPE_CACHE=~/.cache/modelscope` to specify a cache path, or check your network.

**Q: Can I use a different AI service?**
A: Yes. Any service with an OpenAI-compatible API works. Just change `OPENAI_BASE_URL` and `AI_MODEL` in `.env`.
