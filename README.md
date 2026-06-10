# SyncListen

**A minimalist voice-to-text workflow — one run, one session, no auto-save.**

[中文版 / Chinese](README.zh-CN.md)

---

## What is it

SyncListen turns spoken words into polished, copy-ready text in the fewest keystrokes possible. It is designed for Sway/Linux but runs anywhere a microphone, Python, and a clipboard tool exist.

The whole UX is one screen, one keystroke per action, no menus:

| Key | Mode | What it does |
|---|---|---|
| `Enter` | **Write** | Record → transcribe → AI polish → append → auto-copy |
| `E` | **Edit** | Open current content in `$EDITOR` |
| `A` | **AI instruction** | Speak an instruction → AI rewrites the current content |
| `F` | **Term repair** | AI scans the whole text and fixes voice-recognition errors (supports user-supplied reference terms and `wrong=right` pairs) |
| `T` | **Terminology** | Add/remove/list the persistent reference-term list used by `F` |
| `D` | **Clear** | Empty the current content (undoable) |
| `Z` / `X` | Undo / Redo | Step backward / forward through the history stack |
| `C` | **Copy** | Manually copy the current content to clipboard |
| `Q` | **Quit** | Exit the session (nothing is auto-saved) |

The terminology list (managed via `T`) is the only thing persisted between runs.

## Quick start

```bash
git clone https://github.com/ZiJie-Duan/SyncListen.git ~/SyncListen
cd ~/SyncListen

# 1. System packages
sudo apt install libportaudio2 xclip wl-clipboard

# 2. Python deps (install CPU-only torch first if you have no NVIDIA GPU)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# 3. (Optional) AI key
echo 'OPENAI_API_KEY=sk-...' > .env

# 4. (Optional) Editor enhancement — see "Edit mode enhancement" below
bash scripts/setup-editor-nvim.sh

# 5. Run
./run.sh
```

The first launch downloads the SenseVoiceSmall ASR model (~300 MB) into `~/.cache/modelscope/`. One-time cost.

## Stack overview

Everything SyncListen depends on, in one table.

| Layer | Component | Source | License | Purpose |
|---|---|---|---|---|
| Runtime | Python ≥ 3.9 | system | PSF | language runtime |
| System | `libportaudio2` | apt | MIT | PortAudio runtime for `sounddevice` |
| System | `xclip` | apt | GPL-2 | X11 clipboard backend |
| System | `wl-clipboard` | apt | GPL-2 | Wayland clipboard backend |
| Python | `funasr` | PyPI | MIT | Inference framework for SenseVoice |
| Python | `torch`, `torchaudio` | PyPI | BSD-3 | Tensor backend (CPU build recommended on no-GPU hosts) |
| Python | `numpy` | PyPI | BSD-3 | Audio buffer math |
| Python | `sounddevice` | PyPI | MIT | Microphone capture |
| Python | `openai` | PyPI | Apache-2.0 | OpenAI-compatible API client |
| Python | `python-dotenv` | PyPI | BSD-3 | `.env` loader |
| Python | `modelscope` | PyPI | Apache-2.0 | Model downloader for SenseVoice |
| Model | **SenseVoiceSmall** | ModelScope, auto-downloaded on first run | Apache-2.0 | Multilingual ASR (zh / en / yue / ja / ko) |
| AI | OpenAI-compatible chat API (default: DeepSeek `deepseek-chat`) | per-vendor | per-vendor | Polishing in Write mode, instructions in A mode, term repair in F mode |
| Editor (optional) | Neovim ≥ 0.10 | upstream | Apache-2.0 | Backend for `[E]` edit mode |
| Editor (optional) | `folke/lazy.nvim` | GitHub | Apache-2.0 | Plugin manager (auto-bootstrapped) |
| Editor (optional) | `folke/flash.nvim` | GitHub | Apache-2.0 | Search-label jumping |
| Editor (optional) | `mozillazg/pinyin-data` | GitHub | MIT | Source for the embedded Han→pinyin-initial table at `etc/nvim/lua/pyinitial_data.lua` |

## Persistent state

SyncListen never auto-saves session content, but it does maintain a few on-disk artifacts. None contain secrets; all are safe to back up wholesale.

| Path | What lives there | Safe to delete? |
|---|---|---|
| `~/.config/synclisten/terminology.json` | Reference-term list (managed via `T`) | You lose your custom term list |
| `~/.local/state/synclisten/edit/` | Edit-mode drafts. Kept on abnormal editor exit so nothing is lost; pruned to the most recent 20 | Yes — only unrecovered drafts |
| `~/.cache/modelscope/` | SenseVoiceSmall model weights (~300 MB) | Yes — re-downloaded on next run |
| `~/.config/synclisten-nvim/` | Optional Neovim profile for `[E]` mode | Yes — re-run `scripts/setup-editor-nvim.sh` |
| `~/.local/share/synclisten-nvim/lazy/` | Plugins managed by lazy.nvim | Yes — auto re-fetched on next nvim start |
| `~/.local/state/synclisten-nvim/undo/` | Persistent undo history of edited drafts | Yes — but undo across past sessions is lost |

The edit-mode draft directory and the persistent undo store are the two layers of recovery insurance for `[E]` mode: if Neovim crashes, the draft file is **not** deleted, and Neovim's undo file lets you replay every change.

## Edit mode enhancement (optional)

Out of the box, `[E]` opens whatever `$EDITOR` points at, falling back to `vi`. The repo also ships an opinionated, sandboxed Neovim profile that gives you a flash.nvim-powered jump-to-word experience, including **Chinese-aware pinyin-initial jumping** without learning a shuangpin (双拼) layout.

> **A note on the helper scripts.** `scripts/setup-editor-nvim.sh` and `scripts/gen-pyinitial-data.py` were validated only on the author's setup (Pop!_OS / Linux, bash, Neovim ≥ 0.10). On other distributions, shells, or Neovim versions they may need small adjustments — different package names, missing dependencies, paths that differ from the XDG defaults, etc.
>
> If a script fails for you, **don't try to debug it by hand**. Each script is short, self-contained, and easy for an AI assistant (Claude, ChatGPT, …) to patch. Paste the script's source plus the exact error output into the assistant and ask "please make this work on \<my OS / shell / nvim version\>". You should not need to understand pinyin tables, vim internals, or lazy.nvim's bootstrap protocol to get the editor enhancement working.

### What you get

- A dedicated Neovim profile under `~/.config/synclisten-nvim/`, isolated from your everyday `~/.config/nvim/` via `NVIM_APPNAME`.
- `flash.nvim` mapped to `s` in normal/visual/operator-pending modes, configured so a small **pinyin-initial matcher** (`etc/nvim/lua/pyinitial.lua`) expands every typed letter into a vim character class containing all Chinese characters whose pinyin starts with that letter — so `s zw` jumps to "中文", "找位", and any other 2-character Chinese run beginning with z + w initials.
- **Uppercase jump labels** (`A`–`Z`) so lowercase pinyin input never collides with label keys.
- Persistent undo enabled, swap files disabled — drafts stay recoverable.

### Install

```bash
# 1. Install Neovim ≥ 0.10
sudo apt install neovim
# or fetch a recent build:
curl -LO https://github.com/neovim/neovim/releases/latest/download/nvim-linux-x86_64.appimage
chmod +x nvim-linux-x86_64.appimage && sudo mv nvim-linux-x86_64.appimage /usr/local/bin/nvim

# 2. Install the SyncListen profile (idempotent)
bash scripts/setup-editor-nvim.sh

# 3. Add to your shell rc (~/.bashrc / ~/.zshrc / fish config)
export EDITOR='env NVIM_APPNAME=synclisten-nvim nvim'

# 4. Reload your shell, run SyncListen, press E
```

### Use

| Input | Effect |
|---|---|
| `s` | Activate flash.nvim search |
| `s z` | Highlight every Chinese char whose pinyin starts with `z` (中, 找, 在, 自…), plus literal `z`/`Z` |
| `s zw` | Highlight every "z-initial char + w-initial char" run (中文, 找位, 自我…) |
| `Shift+<label>` | Jump to a labeled match (labels are uppercase to avoid colliding with lowercase pinyin input) |
| `v` then `s zw` | Same, but extend the visual selection — your standard "select a Chinese word" move |
| `s abc` | Falls back to literal ASCII matching, so code/English still works the way you expect |
| `<leader>w` (`Space w`) | `:wq` — save and return to SyncListen |

### Layout

```
etc/nvim/
├── init.lua                  # Profile entry; bootstraps lazy.nvim, sets undo/swap policy, loads flash
└── lua/
    ├── pyinitial.lua         # The flash search.mode hook
    └── pyinitial_data.lua    # Embedded Han→pinyin-initial table (~77 KB, 25 721 chars in U+4E00–U+9FFF)
```

`pyinitial_data.lua` is generated from `mozillazg/pinyin-data`. End users do **not** need to regenerate it. Maintainers can refresh the table with:

```bash
python3 scripts/gen-pyinitial-data.py
# or, from a local copy of pinyin.txt:
python3 scripts/gen-pyinitial-data.py --source /path/to/pinyin.txt
```

## Sway integration (optional)

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

`Super+P` opens (or focuses) a 20%-wide foot terminal running SyncListen.

## Environment variables

| Variable | Required | Default | Effect |
|---|---|---|---|
| `OPENAI_API_KEY` | optional | — | If unset, `[A]` and `[F]` are unavailable; Write mode falls back to raw transcription with no AI polish |
| `OPENAI_BASE_URL` | optional | `https://api.deepseek.com/v1` | Any OpenAI-compatible endpoint |
| `AI_MODEL` | optional | `deepseek-chat` | Model name passed to the AI endpoint |
| `DASHSCOPE_API_KEY` | optional | — | Alibaba Cloud key for online streaming transcription (Paraformer). If unset, transcription always uses the local SenseVoice model |
| `PARAFORMER_MODEL` | optional | `paraformer-realtime-v2` | DashScope real-time ASR model used for online transcription |
| `EDITOR` | optional | `vi` | Editor binary used by `[E]`. Set to `env NVIM_APPNAME=synclisten-nvim nvim` for the editor enhancement |
| `VISUAL` | optional | — | Honored as a fallback if `EDITOR` is unset |
| `XDG_STATE_HOME` | optional | `~/.local/state` | Root of the edit-draft directory |
| `MODELSCOPE_CACHE` | optional | `~/.cache/modelscope` | SenseVoice model cache |
| `WAYLAND_DISPLAY` / `DISPLAY` | auto | — | Detected at runtime to choose `wl-copy` vs `xclip` |

The `.env` file at the repo root is loaded via `python-dotenv` and is the recommended place for `OPENAI_*` and `AI_MODEL`.

## Online / Offline modes

SyncListen probes the network once at startup and adapts automatically:

- **Online** — cloud streaming transcription (Alibaba Cloud Paraformer, audio is streamed while you speak for minimal latency) plus all AI features. The local SenseVoice model is **not** loaded, so startup is fast.
- **Offline** — entered when the network is unreachable. A `🔴 离线模式` banner appears, AI hotkeys (`[S]`/`[A]`/`[F]`) are hidden, and transcription falls back to the local SenseVoice model (lazy-loaded on first use, ~5 s once).

Switching is automatic: an online operation that fails triggers a probe to confirm the drop; while offline, every action re-probes so it switches back the moment the network recovers. Install the cloud SDK with `pip install dashscope`.

**Getting a DashScope (Paraformer) API key:**
1. Sign in at the [Alibaba Cloud Model Studio / DashScope console](https://dashscope.console.aliyun.com/) (百炼).
2. Activate the speech (语音) service and create an API Key under **API-KEY 管理**.
3. Put it in `.env`: `echo 'DASHSCOPE_API_KEY=sk-...' >> .env`.

Without `DASHSCOPE_API_KEY`, the app stays usable but transcription always uses the local model.

## FAQ

**Can I run without an AI key?** Yes. Write mode emits raw transcription; `A` and `F` are disabled. Everything else (record, transcribe, edit, undo, terminology) still works.

**The model download is slow / fails.** ModelScope uses a regional mirror by default. Set `MODELSCOPE_CACHE` to retry into a fresh directory, or pre-fetch the model with the `modelscope` CLI before running.

**Can I use a non-DeepSeek backend?** Yes — anything OpenAI-compatible. Set `OPENAI_BASE_URL` and `AI_MODEL` in `.env`.

**An edit crashed Neovim. Did I lose work?** No. The draft file at `~/.local/state/synclisten/edit/edit-<timestamp>.md` is preserved on any abnormal exit and the path is shown in the SyncListen status line. Open it in any editor; persistent undo history is at `~/.local/state/synclisten-nvim/undo/`.

**Can I use the pinyin matcher with my regular nvim config?** Yes. Copy `etc/nvim/lua/pyinitial.lua` and `etc/nvim/lua/pyinitial_data.lua` into your runtime path and route `flash.jump`'s `search.mode` option to `require("pyinitial").mode`. `init.lua` is a worked example.

## Acknowledgements

This project stands on the shoulders of:

- [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) and [funasr](https://github.com/modelscope/FunASR) for the local ASR pipeline.
- [DeepSeek](https://www.deepseek.com/) for an affordable OpenAI-compatible chat API.
- [folke/lazy.nvim](https://github.com/folke/lazy.nvim) and [folke/flash.nvim](https://github.com/folke/flash.nvim) for the editor enhancement.
- [mozillazg/pinyin-data](https://github.com/mozillazg/pinyin-data) for the public-domain-grade pinyin table that powers `pyinitial_data.lua`.

## License

See [LICENSE](LICENSE).
