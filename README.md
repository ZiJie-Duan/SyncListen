# SyncListen

**A minimalist voice-to-text workflow — crash-safe: your session and recordings are always on disk.**

[中文版 / Chinese](README.zh-CN.md)

---

## What is it

SyncListen turns spoken words into polished, copy-ready text in the fewest keystrokes possible. It is designed for Sway/Linux but runs anywhere a microphone, Python, and a clipboard tool exist.

The whole UX is one screen, one keystroke per action, no menus:

| Key | Mode | What it does |
|---|---|---|
| `Enter` | **Write** | Record (level + timeline HUD) → streaming transcription (words appear as you speak) → AI polish → append → auto-copy |
| `S` | **Sublimate** | Same as Write, but with deep AI rewriting: spoken → written style, tighter and more logical (online only) |
| `E` | **Edit** | Open current content in `$EDITOR` |
| `A` | **AI instruction** | Speak an instruction → AI rewrites the current content |
| `M` | **Memory** | View/edit/clear the long-term memory used as recognition context (persisted across restarts) |
| `D` | **Clear** | Empty the current content (undoable); the document snapshot is archived into long-term memory |
| `Z` / `X` | Undo / Redo | Step backward / forward through the history stack. One `Z` right after launch brings back the previous session's content |
| `C` | **Copy** | Manually copy the current content to clipboard |
| `Q` | **Quit** | Exit. Content is saved on every action; press `Z` on the next launch to bring it back |

See [Crash safety](#crash-safety) for what is persisted and how.

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

## Crash safety

Three ground rules: **the program may fail, but it does not exit; it may crash, but nothing already recorded is ever lost.**

- **Document and history are persisted immediately** — every write / undo / redo / clear atomically hits disk (temp file + fsync + atomic replace). **Each launch starts with a blank screen; the previous content sits one `Z` away in the history** — normal quit, Ctrl+C and crashes are all the same case. The history keeps the most recent 200 versions (`SESSION_HISTORY_LIMIT`). A failed save (disk full) never blocks an action: the content stays in memory and still goes to the clipboard, and a banner warns until saving works again.
- **Audio is journaled to disk while recording** — from the first block, a dedicated thread streams audio into a `.pcm` journal (unbuffered writes straight to the kernel, fsync every second); on stop it is atomically converted to a WAV archive (`~/.local/state/synclisten/audio/`). A process crash loses nothing; a power loss costs at most the last ~1 s (`JOURNAL_SYNC_SECONDS`). If the program dies mid-recording, the next launch converts the journal to `*.keep.wav` and shows the path on the first screen.
- **Failed takes are protected** — recordings whose transcription failed, was interrupted or crashed are renamed `*.keep.wav` and excluded from pruning; ordinary recordings are pruned to the most recent 50 (`AUDIO_KEEP_COUNT`).
- **Streaming transcription, words as you speak** — online, the default engine is **Qwen3-ASR-Flash-Realtime** (a true WebSocket stream): a draft appears in the middle pane about 0.3 s after you speak (the de-emphasised `⋯` line), and the server-side VAD finalises each sentence at the next pause; today's memory and the existing document are injected as context. Stopping only waits for the last sentence to finalise (usually < 1 s).
- **A dropped stream loses nothing** — if the realtime session cannot be opened or drops mid-way, recording hot-switches to **chunked transcription** (~5 s chunks, `CHUNK_TARGET_SECONDS`, cut at the quietest 200 ms within the last second of each window — lowest energy, no threshold) and only re-transcribes the audio that has no finalised sentence yet: nothing finalised is lost or duplicated. A chunk that fails in the cloud falls back to the local model on its own. `ONLINE_ASR_ENGINE=qwen` pins chunked mode. The AI polish step runs once, after everything is transcribed.
- **Recording HUD** — while recording, a set of absolute-scale instruments (−60 to 0 dBFS, the usual range of a recording meter; no auto-gain) sits below the document: a level bar with VU ballistics and peak hold, carrying tick marks at −20 and −6 dBFS; with enough rows, a **recording timeline** — one cell per time slice showing that slice's real RMS, with the part already covered by finalised transcription in normal ink and the still-owed tail dimmed, valleys for pauses and `╹` for each sentence boundary, so you can see at a glance how far transcription is behind. No microphone signal means an empty bar and a flat baseline. The same line shows elapsed time, live sentence count / chunk progress and recognised characters, and warns on dropped frames, journal write failures or a dropped stream.
- **Errors never exit** — any command that throws shows the error and a log path (`~/.config/synclisten/logs/`) and returns to the main screen. Ctrl+C while recording stops the recording (its content is still transcribed); during transcription it keeps what has been recognized so far; at the menu it exits. `SIGTERM` / closing the terminal wraps up safely and exits. Stopping the recording (WAV write) and committing (session save) run in a critical section that signals cannot interrupt.

## Interface & effects

Pure ANSI escapes + numpy — no UI framework. Widths are computed in display columns (CJK counts as 2), so nothing misaligns even in a 20 %-wide Sway pane.

**Light and dark only, no hues.** Hierarchy comes from a 24-step grey ink ramp, weight, hairlines and whitespace; motion comes from physical quantities rather than rainbow gradients — the meter has VU ballistics, the peak falls at the PPM rate, and a specular highlight sweeps the title like light across brushed metal. Online/offline is `●` vs `○`, warnings are weight plus `▲`: no information is carried by colour alone.

- **Splash screen** — a letter-spaced wordmark with a cat going about its business beside it + boot log (previous content, recovered recordings, network probe); the probe runs in a background thread so startup is not slowed down.
- **Recording HUD** — breathing `● REC` timer; level bar with VU ballistics (IEC 60268-17, 99 % in 300 ms), sub-cell fill, peak hold (IEC 60268-18, 20 dB / 1.7 s) and absolute tick marks at −20 / −6 dBFS; the status area shows *speaking* while the server-side VAD hears speech.
- **Recording timeline (the tape)** — HUD rows 2–3, one cell per time slice (half the sentence-final silence, so any pause spans at least two cells). Bar height is that slice's real RMS on the same absolute dBFS scale; **the part covered by finalised transcription is normal ink, the still-owed tail is dimmed**, and the boundary between them is exactly how far transcription is behind. Valleys are pauses; `╹` on the baseline marks a VAD sentence boundary. It is an instrument, not decoration.
- **Typewriter output** — recognised text is revealed character by character, the last line carrying a trail (the newest characters glow); in realtime mode the not-yet-final draft shows as a dimmed italic `⋯` line and merges into the text once finalised.
- **The cat** — modelled on oneko (the X11 cat that chases your pointer): not an icon but a 3-row × 7-column creature with a body and poses. It walks (alternating feet, swinging tail), scratches the wall before turning around, sits when it stops, stretches after sitting a while, curls up and sleeps (with a drifting `z`) when nothing happens, and perks its ears (`( O.O ) !`) the moment something does. The same behaviour drives it on the splash screen, in waiting animations, on the recording lane and in the screensaver.
- **Recording lane** — when the terminal is tall enough (HUD budget ≥ 5 rows), the cat gets a lane under the timeline: it perks up and walks while you speak or a new sentence lands, and falls asleep after twenty quiet seconds.
- **Waiting animations** — during AI polish / refine / instruction and the realtime wrap-up: a sweeping label, a timer and the cat pottering about below; AI calls run in a background thread so Ctrl+C interrupts them at any time (the raw transcript is written instead).
- **Commit reveal** — on a successful write, the new text is revealed character by character and the status line sweeps twice.
- **Cat-matrix screensaver** — after `FX_IDLE_SECONDS` (default 120 s) idle on the main screen: a monochrome digital rain with the cat strolling through it (solid — the rain does not leak through its belly; it changes height when it turns around), and a pair of blinking eyes lighting up in the dark now and then. Any key wakes it (that key is not treated as a command).
- **Turning it off** — `SYNCLISTEN_FX=0` (or `NO_COLOR`) gives a plain-text UI (tick marks, timeline and sentence marks stay readable); `FX_IDLE_SECONDS=0` disables only the screensaver.

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
| AI | OpenAI-compatible chat API (default: DeepSeek `deepseek-chat`) | per-vendor | per-vendor | Polishing in Write mode and instructions in A mode |
| Editor (optional) | Neovim ≥ 0.10 | upstream | Apache-2.0 | Backend for `[E]` edit mode |
| Editor (optional) | `folke/lazy.nvim` | GitHub | Apache-2.0 | Plugin manager (auto-bootstrapped) |
| Editor (optional) | `folke/flash.nvim` | GitHub | Apache-2.0 | Search-label jumping |
| Editor (optional) | `mozillazg/pinyin-data` | GitHub | MIT | Source for the embedded Han→pinyin-initial table at `etc/nvim/lua/pyinitial_data.lua` |

## Persistent state

Everything SyncListen writes to disk is listed below. None of it contains secrets; all of it is safe to back up wholesale.

| Path | What lives there | Safe to delete? |
|---|---|---|
| `~/.local/state/synclisten/session.json` | Session document + full history stack (saved atomically on every action) | You start from a blank session; a corrupted file is moved aside as `.corrupt-*` |
| `~/.local/state/synclisten/audio/` | WAV archive of every recording; ordinary files are pruned to the most recent 50 (`AUDIO_KEEP_COUNT`), while `*.keep.wav` (failed / interrupted / crash-recovered) are never auto-deleted | You lose past recordings |
| `~/.local/state/synclisten/edit/` | Edit-mode drafts. Kept on abnormal editor exit so nothing is lost; pruned to the most recent 20 | Yes — only unrecovered drafts |
| `~/.config/synclisten/memory.json` | Long-term memory (view via `M`, persisted across restarts) | Memory is lost (auto-backed up as `.bak` on clear) |
| `~/.config/synclisten/logs/` | Exception logs (crash/error scenes) | Yes |
| `~/.config/synclisten/terminology.json` | (Legacy) old reference-term list; no longer read or written | Yes |
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
| `OPENAI_API_KEY` | optional | — | If unset, `[S]` and `[A]` are unavailable; Write mode falls back to raw transcription with no AI polish |
| `OPENAI_BASE_URL` | optional | `https://api.deepseek.com/v1` | Any OpenAI-compatible endpoint |
| `AI_MODEL` | optional | `deepseek-chat` | Model name passed to the AI endpoint |
| `DASHSCOPE_API_KEY` | optional | — | Alibaba Cloud (Model Studio) key enabling online transcription (Qwen3-ASR-Flash). If unset, transcription always uses the local SenseVoice model |
| `ONLINE_ASR_ENGINE` | optional | `qwen-realtime` | Online engine: `qwen-realtime` (true streaming + context, hot-switches to chunked on failure), `qwen` (chunked + context) or `paraformer` (streaming, no context) |
| `CHUNK_TARGET_SECONDS` | optional | `5` | Target chunk length for chunked transcription (`qwen` engine / offline / realtime fallback), in seconds; actual cut points land on speech pauses |
| `QWEN_ASR_REALTIME_MODEL` | optional | `qwen3-asr-flash-realtime` | Realtime recognition model |
| `DASHSCOPE_REALTIME_URL` | optional | Beijing wss endpoint | Realtime WebSocket endpoint (Singapore: `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime`) |
| `QWEN_ASR_LANGUAGE` | optional | empty (auto) | Realtime recognition language: empty = auto-detect (use it for mixed Chinese/English); or `zh` / `en` / `yue` / `ja` … |
| `REALTIME_VAD_SILENCE_MS` | optional | `400` | Server-side VAD end-of-sentence silence (vendor recommendation 400, range 200–6000) |
| `REALTIME_VAD_THRESHOLD` | optional | `0.0` | Server-side VAD sensitivity (vendor recommendation 0.0, range −1–1) |
| `SYNCLISTEN_FX` | optional | `1` | Set to `0` to disable the ink ramp and animations (a non-empty `NO_COLOR` does the same) |
| `FX_IDLE_SECONDS` | optional | `120` | Idle seconds on the main screen before the cat-matrix screensaver; `0` disables it |
| `AUDIO_KEEP_COUNT` | optional | `50` | How many ordinary recording WAVs to keep; pruned oldest-first at startup (`0` = keep all; `*.keep.wav` is never pruned) |
| `JOURNAL_SYNC_SECONDS` | optional | `1` | fsync interval for the recording journal, in seconds — the most audio a power loss can cost |
| `SESSION_HISTORY_LIMIT` | optional | `200` | Maximum number of versions kept in the session (undo) history |
| `SESSION_FILE` | optional | `~/.local/state/synclisten/session.json` | Session snapshot path |
| `AUDIO_DIR` | optional | `~/.local/state/synclisten/audio` | Recording WAV directory |
| `MEMORY_FILE` | optional | `~/.config/synclisten/memory.json` | Long-term memory path |
| `EDITOR` | optional | `vi` | Editor binary used by `[E]`. Set to `env NVIM_APPNAME=synclisten-nvim nvim` for the editor enhancement |
| `VISUAL` | optional | — | Honored as a fallback if `EDITOR` is unset |
| `XDG_STATE_HOME` | optional | `~/.local/state` | Root of the session snapshot / recordings / edit-draft directories |
| `MODELSCOPE_CACHE` | optional | `~/.cache/modelscope` | SenseVoice model cache |
| `WAYLAND_DISPLAY` / `DISPLAY` | auto | — | Detected at runtime to choose `wl-copy` vs `xclip` |

The `.env` file at the repo root is loaded via `python-dotenv` and is the recommended place for `OPENAI_*` and `AI_MODEL`.

## Online / Offline modes

SyncListen probes the network once at startup and adapts automatically:

- **Online** — cloud transcription via **Qwen3-ASR-Flash-Realtime** (a true WebSocket stream: words appear as you speak, sentences are finalised at pauses, with context injection for proper nouns and mixed Chinese/English; a dropped stream hot-switches to ~5 s chunked transcription via Qwen3-ASR-Flash; `ONLINE_ASR_ENGINE=qwen` pins chunked mode, `=paraformer` selects the Paraformer stream) plus all AI features. The local SenseVoice model is **not** loaded, so startup is fast.
- **Offline** — entered when the network is unreachable. A `🔴 离线模式` banner appears, AI hotkeys (`[S]`/`[A]`) are hidden, and transcription falls back to the local SenseVoice model (lazy-loaded on first use, ~5 s once). A network drop mid-recording switches remaining chunks to the local model automatically.

Switching is automatic: an online operation that fails triggers a probe to confirm the drop; while offline, every action re-probes so it switches back the moment the network recovers. Install the cloud SDK with `pip install dashscope`.

**Getting a DashScope (Paraformer) API key:**
1. Sign in at the [Alibaba Cloud Model Studio / DashScope console](https://dashscope.console.aliyun.com/) (百炼).
2. Activate the speech (语音) service and create an API Key under **API-KEY 管理**.
3. Put it in `.env`: `echo 'DASHSCOPE_API_KEY=sk-...' >> .env`.

Without `DASHSCOPE_API_KEY`, the app stays usable but transcription always uses the local model.

## FAQ

**Can I run without an AI key?** Yes. Write mode emits raw transcription; `S` and `A` are disabled. Everything else (record, transcribe, edit, undo) still works.

**The program crashed / the power died. Did I lose anything?** No. The document and its history are saved to disk on every action; the next launch starts with a blank screen, and one `Z` brings back exactly what you had. Recording audio is journaled to disk while you speak; on startup, any journal left by a crash is converted to `*.keep.wav` and its path is shown in the first screen's status area. See [Crash safety](#crash-safety).

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
