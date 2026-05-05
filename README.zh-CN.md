# SyncListen

**极简语音工作流 —— 一次运行，一个会话，退出不保存。**

[English](README.md)

---

## 是什么

SyncListen 用最少的按键把你说出口的话变成整理好的文字。为 Sway/Linux 设计，但只要有麦克风、Python 和剪贴板工具，到哪都能跑。

整个 UI 就是一个屏幕、一键一动作、没有菜单：

| 键 | 模式 | 作用 |
|---|---|---|
| `回车` | **写入** | 录音 → 转写 → AI 润色 → 追加 → 自动复制剪贴板 |
| `E` | **编辑** | 用 `$EDITOR` 打开当前内容编辑 |
| `A` | **AI 指令** | 语音输入指令 → AI 按指令重写当前内容 |
| `F` | **词语修复** | AI 扫全文修正语音转写错词（支持补充参考词与 `错=对` 替换配对） |
| `T` | **易错词** | 增/删/查 持久化的参考词表（`F` 用得到） |
| `D` | **清空** | 清空当前内容（可撤销） |
| `Z` / `X` | 撤销 / 重做 | 在历史栈中前后翻 |
| `C` | **复制** | 手动复制当前内容到剪贴板 |
| `Q` | **退出** | 退出会话（不会自动保存） |

退出后只有 `T` 管理的易错词表会跨会话保留。

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
| AI 服务 | OpenAI 兼容聊天 API（默认 DeepSeek `deepseek-chat`） | 各厂商 | 各厂商 | 写入模式润色、A 模式指令、F 模式词语修复 |
| 编辑器（可选） | Neovim ≥ 0.10 | 上游 | Apache-2.0 | `[E]` 编辑模式后端 |
| 编辑器（可选） | `folke/lazy.nvim` | GitHub | Apache-2.0 | 插件管理器（脚本自动安装） |
| 编辑器（可选） | `folke/flash.nvim` | GitHub | Apache-2.0 | 搜索标签跳转 |
| 编辑器（可选） | `mozillazg/pinyin-data` | GitHub | MIT | `etc/nvim/lua/pyinitial_data.lua` 的拼音首字母表数据来源 |

## 持久化路径

SyncListen 不会主动保存会话内容，但磁盘上仍有几处工件。它们都不含敏感信息，可以整体备份。

| 路径 | 内容 | 删除影响 |
|---|---|---|
| `~/.config/synclisten/terminology.json` | 易错词表（`T` 管理） | 自定义词表丢失 |
| `~/.local/state/synclisten/edit/` | 编辑草稿。编辑器异常退出时**不会**被删除；保留最近 20 份 | 仅丢失尚未提交的草稿 |
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
| `OPENAI_API_KEY` | 可选 | — | 不填则 `[A]` `[F]` 不可用，写入模式回退为原始转写 |
| `OPENAI_BASE_URL` | 可选 | `https://api.deepseek.com/v1` | 任意 OpenAI 兼容服务地址 |
| `AI_MODEL` | 可选 | `deepseek-chat` | 传给 AI 的 model 名 |
| `EDITOR` | 可选 | `vi` | `[E]` 模式调用的编辑器；推荐设 `env NVIM_APPNAME=synclisten-nvim nvim` |
| `VISUAL` | 可选 | — | `EDITOR` 未设时的备选 |
| `XDG_STATE_HOME` | 可选 | `~/.local/state` | 编辑草稿目录的根 |
| `MODELSCOPE_CACHE` | 可选 | `~/.cache/modelscope` | SenseVoice 模型缓存目录 |
| `WAYLAND_DISPLAY` / `DISPLAY` | 自动 | — | 运行时探测，决定用 `wl-copy` 还是 `xclip` |

仓库根目录的 `.env` 会被 `python-dotenv` 自动加载，是放 `OPENAI_*` 和 `AI_MODEL` 的推荐位置。

## 常见问题

**没有 AI Key 能用吗？** 能。写入模式直接输出原始转写文字；`A` 和 `F` 不可用。其他功能（录音、转写、编辑、撤销、易错词管理）照常。

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
