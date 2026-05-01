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

### 技术栈

| 模块 | 用途 | 方案 |
|------|------|------|
| 语音识别 | 语音 → 文字 | [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) (阿里达摩院，中文/英文优化) |
| AI 处理 | 润色、指令执行 | DeepSeek / OpenAI 兼容 API |
| 录音 | 麦克风采集 | sounddevice (PortAudio) |
| 剪贴板 | 自动复制结果 | wl-copy (Wayland) / xclip (X11) |
| UI | 终端界面 | 纯文本，自动适配终端宽度 |

### 安装

**系统依赖**

```bash
# Ubuntu/Debian/Pop!_OS
sudo apt install libportaudio2 xclip wl-clipboard
```

**Python 依赖**

```bash
cd ~/SyncListen
pip install -r requirements.txt
```

第一次启动时，funasr 会自动下载 SenseVoiceSmall 模型（约 300MB），只需一次。

**配置 AI API**

在项目根目录创建 `.env`：

```bash
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.deepseek.com/v1   # 可选，默认 DeepSeek
AI_MODEL=deepseek-chat                          # 可选，默认 deepseek-chat
```

### Sway 集成

将以下加入 `~/.config/sway/config`：

```
bindsym $mod+p exec ~/bin/sway-synclisten
```

然后创建 `~/bin/sway-synclisten`（已提供在项目外，可参考配置）。

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

### Tech Stack

| Module | Purpose | Solution |
|--------|---------|----------|
| Speech-to-Text | Voice → text | [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) (Alibaba, optimized for Chinese/English) |
| AI Processing | Polish, execute instructions | DeepSeek / OpenAI-compatible API |
| Recording | Microphone capture | sounddevice (PortAudio) |
| Clipboard | Auto-copy results | wl-copy (Wayland) / xclip (X11) |
| UI | Terminal interface | Plain text, auto-adapts to terminal width |

### Installation

**System deps**

```bash
# Ubuntu/Debian/Pop!_OS
sudo apt install libportaudio2 xclip wl-clipboard
```

**Python deps**

```bash
cd ~/SyncListen
pip install -r requirements.txt
```

On first launch, funasr will auto-download the SenseVoiceSmall model (~300MB). One time only.

**Configure AI API**

Create `.env` in project root:

```bash
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.deepseek.com/v1   # optional, defaults to DeepSeek
AI_MODEL=deepseek-chat                          # optional, defaults to deepseek-chat
```

### Sway Integration

Add to `~/.config/sway/config`:

```
bindsym $mod+p exec ~/bin/sway-synclisten
```

Then create `~/bin/sway-synclisten` (provided outside the project, see your setup).

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
