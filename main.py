#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SyncListen — 极简语音工作流
崩溃不丢字：会话与原始录音全程落盘。启动时屏幕空白，上次内容留在历史里，
按 [Z] 一步找回（正常退出、崩溃、断电一视同仁）。

模式：
  在线模式 — 云端 Qwen3-ASR-Flash-Realtime 真流式转写（边说边出字：草稿约 0.3s
            上屏、停顿后定稿；服务端 VAD 切句；注入记忆/文稿作 context）+ AI 全功能；
            启动不加载本地模型，最快。实时会话建不起来或中途断开 → 自动热切换到
            分段转写（约 CHUNK_TARGET_SECONDS 秒一段、切在停顿处，单段云端失败走本地），
            已定稿的句子不丢、不重复。ONLINE_ASR_ENGINE=qwen 固定用分段；=paraformer
            用 Paraformer 流式。
  离线模式 — 网络不可用时自动回退：本地 SenseVoice 分段转写，AI 功能停用、相关
            快捷键隐藏。
  切换：启动探测一次；在线操作失败即探测确认是否断网；离线时每次操作探测
  是否恢复，自动来回切换。

[回车] 写入模式  — 录音（电平/频谱/示波器 HUD）→ 流式/分段转写 → 忠实清理后追加
                  （离线时直接追加原始转写）
[S]    升华写入  — 同上，AI 深度润色（口语转书面、精炼有逻辑）（仅在线）
[E]    编辑模式  — 用 $EDITOR 直接编辑当前内容
[A]    AI 指令   — 语音指令，AI处理当前内容（仅在线）
[M]    记忆      — 查看/编辑/清空长期记忆（连贯记忆，注入识别上下文，跨重启持久化）
[D]    清空      — 清空当前内容（可撤销）；同时把文稿快照交给长期记忆归档
[Z]    撤销      — 回滚到上一版本（跨启动：启动后按一次即回到上次内容）
[X]    重做      — 前进到下一版本
[C]    复制      — 手动复制到剪贴板
[Q]    退出      — 会话已随每次操作落盘

崩溃安全契约：
  · 每次提交/撤销/重做立即原子写盘（会话 JSON，fsync）；
  · 录音全程由独立线程写磁盘流水账（.pcm），停止即原子转 WAV；无论程序怎么崩，
    录完的音频都能从磁盘找回（启动时自动恢复遗留流水账为 *.keep.wav）；
  · 转写按句/按段进行，单段失败不影响其余；失败/中断件的 WAV 标记保留、不被清理；
  · 任何错误一律提示而非退出；Ctrl+C 录音中 = 停止录音（已录内容照常转写），
    菜单中 = 退出；SIGTERM/SIGHUP = 安全收尾后退出。

视觉效果：SYNCLISTEN_FX=0 或 NO_COLOR 关闭颜色/动画；FX_IDLE_SECONDS 控制屏保。
"""

import collections
import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from synclisten.config import (
    CHUNK_TARGET_SECONDS,
    FX_IDLE_SECONDS,
    LEVEL_SLICE_SECONDS,
    STATE_DIR,
)
from synclisten.core import errlog
from synclisten.core.recorder import (
    AudioRecorder,
    mark_keep,
    prune_wavs,
    recover_orphan_pcms,
    split_at_pauses,
)
from synclisten.core.ai_client import AIClient
from synclisten.core.network import NetworkManager
from synclisten.core.session import Session
from synclisten.core.live_asr import LiveTranscriber
from synclisten.ui import fx
# 注意：SenseVoiceTranscriber / CloudTranscriber / QwenRealtimeTranscriber 均按需延迟
# 导入与实例化，见 TranscribeEngine——在线模式下不加载本地模型，启动更快。

# 当前网络管理器（在 main() 中设置）。redraw 在未显式传 net 时回退到它，
# 以便所有界面（撤销/复制/编辑等）离线时都能保持离线标记一致。
_NET = None
# 收到 SIGTERM/SIGHUP：当前步骤安全收尾后退出主循环。
_QUIT_REQUESTED = False
# 主线程正在高频重绘（录音/收尾/等待动画/屏保）：后台线程此时不得向终端写字。
_SCREEN_BUSY = False

# 实时会话收尾（等服务端补完最后一句）的等待上限（秒）：与 SDK end_session 默认量级一致。
REALTIME_FINISH_TIMEOUT = 10.0
# 启动画面最短展示时长（秒）：纯视觉节奏参数，网络探测在后台并行进行，不拖慢启动。
SPLASH_MIN_SECONDS = 0.6
# 高频重绘的帧间隔（秒）：约 12 fps，动画流畅且终端负载可忽略。
FRAME_SECONDS = 0.08

# ── 跨平台即时按键读取 ──────────────────────────────────
def _is_tty():
    return sys.stdin.isatty()


# 已读到但尚未交给调用方的按键（一次 read 可能带回多个键/半截转义序列）
_key_buffer = collections.deque()


def _split_keys(s):
    """把一次读到的字符串拆成按键序列：转义序列（方向键等）保持完整。"""
    keys, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "\x1b":
            j = i + 1
            if j < n and s[j] in "[O":
                j += 1
                while j < n and not (s[j].isalpha() or s[j] == "~"):
                    j += 1
                j = min(j + 1, n)
            keys.append(s[i:j])
            i = j
        else:
            keys.append(s[i])
            i += 1
    return keys


if sys.platform == "win32":
    import msvcrt

    def _read_key():
        """读取单个按键（阻塞）。

        - 普通字符返回单字符;
        - 单独的 ESC 返回 "\\x1b"(长度 1);
        - 方向键/功能键返回以 "\\x1b" 开头的多字符串(调用方据此区分并忽略)。
        """
        if _key_buffer:
            return _key_buffer.popleft()
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            nxt = msvcrt.getch()
            return "\x1b[" + nxt.decode("latin-1", errors="ignore")
        return ch.decode("utf-8", errors="ignore")

    def _key_ready(timeout):
        if _key_buffer or msvcrt.kbhit():
            return True
        time.sleep(timeout)
        return msvcrt.kbhit()
else:
    import tty
    import termios
    import select

    def _read_key():
        """读取单个按键（阻塞）。须已处于 cbreak 模式（见 _cbreak / _getch_raw）。

        直接 os.read 文件描述符（不经 sys.stdin 的缓冲层）：select 看到的就是
        read 能读到的，不会出现"select 说有、read 却被缓冲截走"的错位。
        一次 read 可能带回多个键，多余的放进 _key_buffer 供下次取用，不丢键。
        """
        if _key_buffer:
            return _key_buffer.popleft()
        fd = sys.stdin.fileno()
        data = os.read(fd, 64)
        if not data:
            return ""  # EOF
        s = data.decode("utf-8", errors="replace")
        if s == "\x1b":
            # 单独 ESC 与转义序列的区分：极短超时探测后续字节
            while select.select([fd], [], [], 0.05)[0]:
                more = os.read(fd, 64)
                if not more:
                    break
                s += more.decode("utf-8", errors="replace")
                if s[-1].isalpha() or s[-1] == "~":
                    break
        keys = _split_keys(s)
        _key_buffer.extend(keys[1:])
        return keys[0]

    def _key_ready(timeout):
        if _key_buffer:
            return True
        return bool(select.select([sys.stdin.fileno()], [], [], timeout)[0])


@contextlib.contextmanager
def _cbreak():
    """让终端在整个录音/收尾/菜单等待期间保持 cbreak（无回显、无行缓冲）。

    进入时用 TCSANOW：不丢弃已按下的键——用 TCSAFLUSH 会把"唤醒 select 的那个
    回车"一并冲掉，表现为回车要按两次。
    """
    if not _is_tty() or sys.platform == "win32":
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _getch_raw():
    """阻塞读取单个按键（自带 cbreak）。"""
    with _cbreak():
        return _read_key()


def _getch():
    """读取单个字符。在 TTY 下即时响应，非 TTY 下退化为 input()。"""
    if _is_tty():
        return _getch_raw()
    # 非交互环境：退化为标准 input()
    try:
        line = input()
        return line[0] if line else "\n"
    except EOFError:
        return ""


def _poll_key(timeout=FRAME_SECONDS):
    """等待单个按键至多 timeout 秒；超时返回 None（高频循环轮询用）。

    须在 _cbreak() 内调用：否则轮询间隙终端处于普通模式，按键会回显到屏幕、
    并被行缓冲。非 TTY 下只是等待。
    """
    if not _is_tty():
        time.sleep(timeout)
        return None
    if not _key_ready(timeout):
        return None
    return _read_key()


def _wait_for_enter():
    """阻塞直到检测到回车/换行（非交互录音路径用）。"""
    if not _is_tty():
        try:
            input()
        except EOFError:
            pass
        return
    with _cbreak():
        while True:
            ch = _read_key()
            if ch in ("\r", "\n", ""):
                break


def _flush_input():
    """丢弃等待期间积压的按键（如润色时多按的回车），免得回到主界面被当成命令。"""
    _key_buffer.clear()
    if not _is_tty():
        return
    try:
        if sys.platform == "win32":
            while msvcrt.kbhit():
                msvcrt.getch()
        else:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


# ── 信号：临界区推迟 + 退出请求 ──────────────────────────

_DEFERRABLE_SIGNALS = tuple(
    s for s in (
        signal.SIGINT,
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGHUP", None),
    ) if s is not None
)


@contextlib.contextmanager
def _defer_signals():
    """临界区（停录写 WAV、提交落盘）：期间到达的 Ctrl+C / SIGTERM / SIGHUP
    推迟到临界区结束后再以 KeyboardInterrupt 抛出，保证落盘动作不被中途打断。
    非主线程调用时不做推迟（signal 只能在主线程设置）。
    """
    global _QUIT_REQUESTED
    fired = []
    olds = {}

    def _handler(signum, frame):
        fired.append(signum)

    if threading.current_thread() is threading.main_thread():
        for s in _DEFERRABLE_SIGNALS:
            olds[s] = signal.signal(s, _handler)
    try:
        yield
    finally:
        for s, h in olds.items():
            signal.signal(s, h)
    if fired:
        if any(s != signal.SIGINT for s in fired):
            _QUIT_REQUESTED = True
        raise KeyboardInterrupt


def _install_signal_handlers():
    """SIGTERM/SIGHUP：置退出标志并以 KeyboardInterrupt 打断当前等待——
    录音中 = 停止录音并把已录内容提交，随后主循环见标志退出；菜单中 = 直接退出。
    都走同一条「先落盘再退」的路。（Ctrl+Z 是终端的挂起快捷键，程序不抢占；
    应用内撤销用 [Z]。）
    """
    def _handler(signum, frame):
        global _QUIT_REQUESTED
        _QUIT_REQUESTED = True
        raise KeyboardInterrupt

    for s in _DEFERRABLE_SIGNALS:
        if s == signal.SIGINT:
            continue
        try:
            signal.signal(s, _handler)
        except (ValueError, OSError):
            pass


def _join_notes(*parts):
    """把非空提示拼接成一行状态文案。"""
    return "  ·  ".join(p for p in parts if p)


# ── 界面绘制 ─────────────────────────────────────────

def _clear():
    try:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    except OSError:
        pass


def _write_frame(lines, soft):
    """输出一帧：soft = 光标回位 + 逐行清尾 + 清屏尾（无闪烁）；否则整屏清除重绘。"""
    if soft:
        out = "\033[H" + "".join(line + "\033[K\n" for line in lines) + "\033[J"
    else:
        out = "\033[2J\033[H" + "".join(line + "\n" for line in lines)
    try:
        sys.stdout.write(out)
        sys.stdout.flush()
    except OSError:
        pass  # 终端已关闭等：显示问题不能中断数据操作


def _term_size():
    """(列数, 行数)。"""
    try:
        ts = shutil.get_terminal_size()
        return ts.columns, ts.lines
    except Exception:
        return 80, 24


def _term_width():
    return _term_size()[0]


def _wrap_text(text, width):
    """将文本按终端显示宽度自动换行（CJK 按 2 列计）。"""
    if not text:
        return []
    return fx.wrap_width(text, width)


def _tail_lines(lines, max_lines):
    """行数超限时只留尾部，首行用 … 标记。"""
    if len(lines) <= max_lines:
        return lines
    if max_lines <= 1:
        return lines[-1:]
    return ["…"] + lines[-(max_lines - 1):]


def _header_line(session, net, w, now=None):
    """标题栏：字标 + 右侧状态（在线/离线 · 字数 · 时间）。"""
    if net is not None and not net.is_online():
        state = fx.paint("○ 离线", fx.HI, bold=True)   # 单色里用空心/实心区分，不用红绿
    else:
        state = fx.paint("● 在线", fx.INK)
    right = (f"{state}  {fx.paint(f'{len(session.content)} 字', fx.MUTED)}"
             f"  {fx.paint(time.strftime('%H:%M'), fx.FAINT)}")
    # 窄面板优先保住右侧状态：先试拉开字距的字标，放不下就用紧凑字标
    for cap in (min(w, 24), 20):
        left = fx.banner(cap, now or 0.0)
        gap = w - fx.display_width(left) - fx.display_width(right)
        if gap >= 1:
            return left + " " * gap + right
    return fx.banner(min(w, 24), now or 0.0)


def _op_bar(net, width):
    """底部操作栏（可能多行）：离线时隐藏依赖网络的 AI 键（[S] 升华 / [A] AI）。
    [M] 记忆模式本身（查看/编辑/清空）是本地操作，离线也可用。
    宽度不够时依次退让：收紧间距 → 折成两行 → 丢掉文字标签只留按键，
    保证每行不超宽（否则终端自动折行会打乱重绘）。"""
    items = [("\u21b5", "写入"), ("S", "升华"), ("E", "编辑"), ("A", "指令"), ("M", "记忆"),
             ("D", "清空"), ("Z", "撤销"), ("X", "重做"), ("C", "复制"), ("Q", "退出")]
    if net is not None and not net.is_online():
        items = [it for it in items if it[0] not in ("S", "A")]

    def _cells(with_label):
        out = []
        for k, label in items:
            key = fx.paint(k, fx.HI, bold=True)
            out.append(f"{key}{fx.paint(' ' + label, fx.MUTED)}" if with_label else key)
        return out

    def _fit(with_label, max_rows):
        """按显示宽度贪心排版；行数超过 max_rows 返回 None。"""
        plains = [(k + (" " + lb if with_label else "")) for k, lb in items]
        cells = _cells(with_label)
        for sep in ("   ", "  ", " "):
            rows, cur, cur_w = [], [], 0
            for i, pl in enumerate(plains):
                w = fx.display_width(pl)
                add = w if not cur else w + len(sep)
                if cur and cur_w + add > width:
                    rows.append(sep.join(cur))
                    cur, cur_w = [cells[i]], w
                else:
                    cur.append(cells[i])
                    cur_w += add
            if cur:
                rows.append(sep.join(cur))
            if len(rows) <= max_rows:
                return rows
        return None

    # 一行放得下就一行；否则两行带标签；再不行只留按键
    return (_fit(True, 1) or _fit(True, 2) or _fit(False, 2) or _fit(False, 99)
            or [fx.paint(items[0][0], fx.HI, bold=True)])


def redraw(session, transient="", hint="", net=None, meter=None, soft=False,
           hud=None, draft="", content=None, now=None):
    """重绘主界面分区：标题栏 + 内容区 + 临时区（实时转写/待润色原文）+ HUD + 操作栏。

    Args:
        transient: 中间区的临时文本（已定稿部分）；行数按终端高度自适应，过长只留尾部。
        draft: 紧跟 transient 之后的草稿文本（实时识别尚未定稿的部分），弱化显示。
        hud: 录音 HUD 行列表（电平/频谱/示波器）；meter 为旧接口（单行）。
        content: 覆盖显示的内容（揭示动画用），None 则显示 session.content。
        soft: True 时无闪烁增量重绘，供高频循环调用；False 时整屏清除重绘。
        net: 可选 NetworkManager。离线时顶部显示离线标记、操作栏隐藏 AI 键。
    显示失败（终端已关闭等）静默忽略——显示问题不能中断数据操作。
    """
    if net is None:
        net = _NET
    w, rows = _term_size()
    head = [_header_line(session, net, w, now)]
    if net is not None and not net.is_online():
        head.append(fx.paint("○ 离线模式 · 本地转写 · AI 功能已停用", fx.HI, bold=True))
    if session.save_error:
        head.append(fx.paint(
            f"▲ 会话落盘失败（{session.save_error}）· 内容仅在内存，请尽快复制并检查磁盘", fx.HI))

    foot = []
    hud_lines = list(hud) if hud else ([meter] if meter is not None else [])
    foot.extend(hud_lines)
    foot.extend(_op_bar(net, w))
    if hint:
        for raw_line in hint.split("\n"):
            # 提示行也按显示宽度折行：交给终端自动折行会让软重绘的行数对不上
            foot.extend(_wrap_text(raw_line, w) if fx.display_width(raw_line) > w else [raw_line])

    # 中间区与内容区分配剩余高度：中间区最多占一半；极矮终端先牺牲提示行，再压缩两区
    transient_lines = _wrap_text(transient, w - 3) if transient else []
    draft_lines = _wrap_text(draft, w - 3) if draft else []
    total_mid = len(transient_lines) + len(draft_lines)
    fixed = len(head) + 2 + 1  # 两条分隔线 + 光标行
    avail = rows - fixed - len(foot)
    while avail < 4 and len(foot) > len(hud_lines) + 1:
        foot.pop()  # 提示行
        avail = rows - fixed - len(foot)
    if total_mid:
        t_show = min(total_mid, max(3, avail // 2))
        t_show = max(1, min(t_show, avail - 2))  # 内容区至少留 1 行
        transient_block = t_show + 1
    else:
        t_show, transient_block = 0, 0
    content_avail = max(1, avail - transient_block)

    shown = session.content if content is None else content
    if shown:
        content_lines = _tail_lines(_wrap_text(shown, w), content_avail)
    else:
        content_lines = [fx.paint("…", fx.VOID)]

    lines = list(head)
    lines.append(fx.rule(w))
    lines.extend(content_lines)
    lines.append(fx.rule(w))

    if transient_block:
        room = t_show
        if transient_lines and draft_lines and room >= 2:
            draft_room = max(1, min(len(draft_lines), room // 2))
            final_room = room - draft_room
        elif draft_lines and (not transient_lines or room < 2):
            final_room, draft_room = 0, room
        else:
            final_room, draft_room = room, 0
        final_show = _tail_lines(transient_lines, final_room) if final_room else []
        draft_show = draft_lines[-draft_room:] if draft_room else []
        first = True
        for i, ln in enumerate(final_show):
            # 最后一行用拖影：刚落笔的字还"热"着，越新越亮
            body = fx.trail(ln) if i == len(final_show) - 1 and not draft_show else fx.paint(ln, fx.INK)
            lines.append(fx.paint("▍ " if first else "  ", fx.FAINT) + body)
            first = False
        for ln in draft_show:
            prefix = "▍ " if first else "⋯ "
            lines.append(fx.paint(prefix, fx.FAINT) + fx.paint(ln, fx.MUTED, italic=True))
            first = False
        lines.append(fx.rule(w))

    lines.extend(foot)
    _write_frame(lines, soft)


def _detect_display_server():
    """检测当前运行在 X11 还是 Wayland。"""
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def copy_to_clipboard(text):
    """根据显示服务器类型写入剪贴板。"""
    if not text:
        return
    ds = _detect_display_server()
    if ds == "wayland":
        subprocess.run(["wl-copy"], input=text.encode(), check=False)
    elif ds == "x11":
        for sel in ("clipboard", "primary"):
            subprocess.run(["xclip", "-selection", sel], input=text.encode(), check=False)
    else:
        # 兜底：两个都试
        subprocess.run(["wl-copy"], input=text.encode(), check=False)
        for sel in ("clipboard", "primary"):
            subprocess.run(["xclip", "-selection", sel], input=text.encode(), check=False)


# ── 转写引擎 ─────────────────────────────────────────

class TranscribeEngine:
    """统一转写引擎：在线走云端 ASR（Qwen 实时 / Qwen 分段 / Paraformer），
    离线或云端失败时回退本地 SenseVoice。

    本地模型懒加载——在线模式从不实例化它，保证启动最快；首次掉线才加载（约 5s）。
    云端识别器无本地模型，轻量，按需创建。

    online_engine：
      "qwen-realtime" —— Qwen3-ASR-Flash-Realtime 流式（默认），边说边出字，带 context；
                         失败自动热切换到分段。
      "qwen"          —— Qwen3-ASR-Flash 一次性识别，按段送识别（边录边转），带 context。
      "paraformer"    —— Paraformer 流式识别，边录边传（保底/对比）。
    """

    def __init__(self, recorder, net, cloud_available):
        self.recorder = recorder
        self.net = net
        self.cloud_available = cloud_available  # 是否配置了 DASHSCOPE_API_KEY
        from synclisten.config import ONLINE_ASR_ENGINE
        self.online_engine = ONLINE_ASR_ENGINE
        self._local = None
        self._cloud = None     # Paraformer 流式识别器
        self._qwen = None      # Qwen3-ASR-Flash 一次性识别器
        self._realtime = None  # Qwen3-ASR-Flash-Realtime 流式识别器

    def get_cloud(self):
        """Paraformer 流式识别器（保底/对比引擎）。"""
        from synclisten.core.cloud_transcriber import CloudTranscriber
        if self._cloud is None:
            self._cloud = CloudTranscriber()
        return self._cloud

    def get_qwen(self):
        """Qwen3-ASR-Flash 一次性识别器（分段引擎，带 context）。"""
        from synclisten.core.qwen_asr import QwenASRTranscriber
        if self._qwen is None:
            self._qwen = QwenASRTranscriber()
        return self._qwen

    def get_realtime(self):
        """Qwen3-ASR-Flash-Realtime 流式识别器（默认在线引擎，带 context）。"""
        from synclisten.core.qwen_realtime import QwenRealtimeTranscriber
        if self._realtime is None:
            self._realtime = QwenRealtimeTranscriber()
        return self._realtime

    def get_local(self):
        from synclisten.core.transcriber import SenseVoiceTranscriber
        if self._local is None:
            self._local = SenseVoiceTranscriber()
        return self._local

    def local_loaded(self):
        return self._local is not None


def _local_transcribe(session, engine, audio):
    """本地 SenseVoice 转写整段音频（首次会触发约 5s 模型加载）。

    仅供 Paraformer 路径兜底使用；Qwen/离线路径的分段本地兜底在
    LiveTranscriber 里。失败抛异常，由调用方保全录音并提示。
    """
    hint = "· 本地转写中…" if engine.local_loaded() else "· 加载本地模型（首次约 5s）…"
    redraw(session, net=engine.net, hint=hint)
    local = engine.get_local()
    text, _ = local.transcribe(audio)
    return text


def _build_capture_context(session, memory_store):
    """拼装 ASR 背景文本（context），提升中英混读与专名识别。

    两件套：① 今天的长期记忆 ② 已写入文稿（过长截最近窗口）。
    Qwen 实时/分段引擎使用；Paraformer 不支持 context，忽略本值。
    """
    from synclisten.config import ASR_CONTEXT_DOC_LIMIT
    from synclisten.core.qwen_asr import CONTEXT_DOC_LABEL, CONTEXT_MEMORY_LABEL
    parts = []
    mem = memory_store.today_text() if memory_store is not None else ""
    if mem:
        parts.append(CONTEXT_MEMORY_LABEL + mem)
    doc = (session.content or "").strip()
    if doc:
        if len(doc) > ASR_CONTEXT_DOC_LIMIT:
            doc = doc[-ASR_CONTEXT_DOC_LIMIT:]
        parts.append(CONTEXT_DOC_LABEL + "\n" + doc)
    return "\n\n".join(parts)


# ── 等待动画：后台干活，前台播动画 ──────────────────────

# 全程就这一只猫：等待动画、启动画面、录音跑道共用它的状态，看起来像同一只
_CAT = fx.Cat(40, roam=True)
_CAT_LANE_MAX = 44


def _cat_lane(cat, width, rows, now, level=None):
    """猫的跑道：终端高度够才给它三行地方跑，否则不画。返回行列表（可能为空）。"""
    if not fx.ENABLED or rows < _MIN_UI_ROWS + fx.CAT_H:
        return []
    lane = max(fx.CAT_W + 2, min(width, _CAT_LANE_MAX))
    cat.resize(lane)
    cat.update(now)
    return [fx.paint(r, fx.FAINT if level is None else level) for r in cat.render(lane, now)]


def _wait_hint(label, t0, now, extra=""):
    """等待提示：一行状态 + 一条猫跑道（猫在下面自己溜达）。"""
    w, rows = _term_size()
    line = (f"{fx.spinner(now)} {fx.sweep(label, now)} "
            f"{fx.paint(fx.elapsed_text(now - t0), fx.FAINT)}")
    if extra:
        line = f"{line}  {fx.paint(extra, fx.FAINT)}"
    return "\n".join([line] + _cat_lane(_CAT, w, rows, now))


def _await_animated(session, net, label, fn, transient_fn=None, draft_fn=None, hud_fn=None):
    """在后台线程执行 fn()，主线程按帧重绘等待动画；返回 fn 的结果，异常原样抛出。

    Ctrl+C 打断等待时抛 KeyboardInterrupt（fn 在后台跑完即丢弃结果，不会写入）。
    transient_fn / draft_fn / hud_fn 为每帧取当前显示内容的回调（可空）。
    """
    box = {}

    def work():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 —— 原样转交给主线程
            box["e"] = e

    t = threading.Thread(target=work, name="await-" + label, daemon=True)
    t.start()
    t0 = time.monotonic()
    if not _is_tty():
        t.join()
    else:
        with _cbreak():
            while t.is_alive():
                now = time.monotonic()
                hint = _wait_hint(label, t0, now)
                redraw(
                    session, net=net,
                    transient=transient_fn() if transient_fn else "",
                    draft=draft_fn() if draft_fn else "",
                    hud=hud_fn() if hud_fn else None,
                    hint=hint, soft=True, now=now,
                )
                t.join(FRAME_SECONDS)
                _poll_key(0)  # 顺带吞掉等待期间的按键
    if "e" in box:
        raise box["e"]
    return box.get("v")


# ── 录音循环（HUD + 流式/分段调度 + 实时上屏）─────────────

_METER_MAX_WIDTH = 24  # 电平条最大格数；窄终端按可用宽度收缩
# HUD 行数预算之外，界面至少需要的行数：标题 1 + 分隔 2 + 内容 3 + 中间区 4 + 操作栏 1 + 提示 1 + 光标 1
_MIN_UI_ROWS = 13


class _CaptureRun:
    """一次录音的转写调度状态：实时会话 / 分段调度器，以及实时→分段的热切换。"""

    def __init__(self, engine, context):
        self.engine = engine
        self.context = context
        self.rt = None        # QwenRealtimeTranscriber（实时路径）
        self.live = None      # LiveTranscriber（分段路径 / 实时失败后的接手者）
        self.cloud = None     # Paraformer
        self.failover_note = ""
        self.typewriter = fx.Typewriter()
        self.peak = fx.PeakHold()
        self.vu = fx.Ballistics()
        self.cat = fx.Cat(40, roam=True)
        self._was_talking = False
        self._seen_units = 0

    def streaming(self):
        return self.rt is not None and not self.rt.failed

    def text(self):
        t = self.rt.final_text() if self.rt is not None else ""
        if self.live is not None:
            t += self.live.text()
        return t

    def draft(self):
        return self.rt.preview_text() if self.streaming() else ""

    def covered_seconds(self):
        """已定稿覆盖到的音频秒数（实时会话 + 接手的分段之和）。

        时间轴用它把"已经识别出来的"和"还欠着的"分开显示：两者的分界就是
        转写落后了多少。
        """
        total = self.rt.covered_seconds() if self.rt is not None else 0.0
        if self.live is not None:
            total += self.live.covered_seconds()
        return total

    def mark_seconds(self):
        """断句落点（秒）：服务端 VAD 判定的句尾位置。"""
        return self.rt.mark_seconds() if self.rt is not None else []

    def nudge_cat(self, now):
        """把"有动静/没动静"喂给猫：开口说话、又出一句字都算动静（边沿触发）。"""
        if self.streaming():
            talking = self.rt.speech_active()
            units = self.rt.segments()
        else:
            talking = False
            units = self.live.done_total()[0] if self.live is not None else 0
        if (talking and not self._was_talking) or units != self._seen_units:
            self.cat.poke(now)
        elif not talking:
            self.cat.rest(now)
        self._was_talking, self._seen_units = talking, units

    def failover_if_needed(self, recorder):
        """实时会话中途失败：定稿未覆盖的音频交给分段调度器接手，录音不中断。"""
        if self.rt is None or not self.rt.failed or self.live is not None:
            return False
        net = self.engine.net
        errlog.log_message("实时识别中断，切换分段转写", self.rt.error)
        if not net.probe():
            net.go_offline()
            self.failover_note = "▲ 网络中断，剩余部分改用本地转写"
        else:
            self.failover_note = "▲ 实时连接中断，剩余部分改用分段转写"
        covered = int(self.rt.covered_seconds() * recorder.samplerate)
        recorder.skip_pending(covered)
        self.live = LiveTranscriber(self.engine, self.context, seed_text=self.rt.final_text())
        self.rt.close()
        return True

    def fallback_for_rest(self, audio, samplerate):
        """停止后才发现实时会话失败：对定稿未覆盖的剩余音频分段兜底。"""
        covered = int(self.rt.covered_seconds() * samplerate)
        rest = audio[covered:] if audio is not None else None
        net = self.engine.net
        errlog.log_message("实时识别收尾失败，剩余音频分段兜底", self.rt.error)
        if not net.probe():
            net.go_offline()
            self.failover_note = "▲ 网络中断，剩余部分改用本地转写"
        else:
            self.failover_note = "▲ 实时连接中断，剩余部分改用分段转写"
        self.live = LiveTranscriber(self.engine, self.context, seed_text=self.rt.final_text())
        if rest is not None and rest.size:
            for piece in split_at_pauses(rest, CHUNK_TARGET_SECONDS, samplerate):
                self.live.submit(piece)


def _hud_lines(recorder, run, started_at, width, budget, now):
    """录音 HUD。

    第一行：呼吸的录音点 + 计时 + 电平条（VU 弹道 + 峰值保持 + 绝对刻度）+ 状态。
    余下行：录音时间轴（"磁带"）——每格一个时间片的电平，已定稿覆盖的部分正常
    墨色、还没定稿的尾巴压暗，谷底是停顿、亮点是断句落点。它不是装饰：说话音量、
    停顿位置、转写落后多少，都从这一条上读出来。

    宽度不够时按"装饰性优先"依次省略状态片段（猫 → 字数 → 段落数），
    告警片段（丢帧/未写盘/实时中断）始终保留；电平条至少 6 格。
    """
    raw = recorder.display_level()
    level = run.vu.update(raw, now)      # 表针有惯性：看着像真表，不是逐帧抖
    peak = run.peak.update(raw, now)     # 峰值跟真实电平，不跟表针
    head_plain = f"● REC {fx.elapsed_text(now - started_at)} "
    head = (f"{fx.rec_dot(now)} {fx.paint('REC', fx.HI, bold=True)} "
            f"{fx.paint(fx.elapsed_text(now - started_at), fx.INK)} ")

    # (plain, styled, prio)；prio 越大越先被丢掉，0 = 永不丢弃
    parts = []
    if run.streaming():
        talking = run.rt.speech_active()
        if talking:
            parts.append(("说话中", fx.paint("说话中", fx.HI, bold=True), 3))
        parts.append((f"{run.rt.segments()}句",
                      fx.paint(f"{run.rt.segments()}句", fx.MUTED), 1))
    elif run.live is not None:
        done, total = run.live.done_total()
        sp = fx.spinner(now)
        parts.append((f"· {done}/{total}段", f"{sp} {fx.paint(f'{done}/{total}段', fx.MUTED)}", 1))
    chars = len(run.text())
    if chars:
        parts.append((f"{chars}字", fx.paint(f"{chars}字", fx.MUTED), 2))
    if run.rt is not None and run.rt.failed:
        parts.append(("▲ 实时→分段", fx.alert("▲ 实时→分段"), 0))
    if recorder.dropouts:
        parts.append((f"▲ 丢帧×{recorder.dropouts}", fx.alert(f"▲ 丢帧×{recorder.dropouts}"), 0))
    if recorder.journal_error:
        parts.append(("▲ 录音未写盘", fx.alert("▲ 录音未写盘", strong=True), 0))

    min_bar = 6

    def _tail_width(ps):
        return sum(fx.display_width(p[0]) + 1 for p in ps)

    # 从装饰性最强的片段开始丢，直到电平条至少 min_bar 格
    while fx.display_width(head_plain) + min_bar + _tail_width(parts) > width:
        droppable = [i for i, p in enumerate(parts) if p[2] > 0]
        if not droppable:
            break
        parts.pop(max(droppable, key=lambda i: parts[i][2]))
    tail = " ".join(p[1] for p in parts)
    bar_w = max(min_bar, min(_METER_MAX_WIDTH,
                             width - fx.display_width(head_plain) - _tail_width(parts) - 1))
    lines = [f"{head}{fx.meter_bar(level, bar_w, peak)} {tail}".rstrip()]

    if budget >= 2:
        tape_rows = 2 if budget >= 3 else 1
        covered = int(run.covered_seconds() / LEVEL_SLICE_SECONDS)
        marks = {int(t / LEVEL_SLICE_SECONDS) for t in run.mark_seconds()}
        lines.extend(" " + ln for ln in fx.render_tape(
            recorder.levels(), max(4, width - 2), covered, tape_rows, marks))
    if budget >= 2 + fx.CAT_H and fx.ENABLED:
        # 猫跑道：说话/出字它就竖耳朵走动，安静久了自己趴下睡
        run.nudge_cat(now)
        lane = max(fx.CAT_W + 2, min(width, _CAT_LANE_MAX))
        run.cat.resize(lane)
        run.cat.update(now)
        lines.extend(fx.paint(r, fx.FAINT) for r in run.cat.render(lane, now))
    return lines


def _hud_budget(rows):
    """按终端高度决定 HUD 行数：电平 1 + 时间轴 2 + 猫跑道 3，至少 1。"""
    return max(1, min(2 + fx.CAT_H, rows - _MIN_UI_ROWS))


def _record_loop(session, engine, net, hint, run):
    """录音期间的主循环：非阻塞收键 + HUD + 流式/分段调度 + 已识别文本实时上屏。

    回车 / 退出信号 → 返回（停止录音，已录内容照常转写）。Ctrl+C 由调用方
    捕获，语义相同。非交互环境退化为阻塞等回车（无 HUD、无中途分段）。
    """
    recorder = engine.recorder
    if not _is_tty():
        _wait_for_enter()
        return
    started = time.monotonic()
    while not _QUIT_REQUESTED:
        key = _poll_key(FRAME_SECONDS)
        if key in ("\r", "\n", ""):
            return
        # 其他按键（含 ESC/方向键）在录音中一律忽略
        run.failover_if_needed(recorder)
        if run.live is not None:
            chunk = recorder.take_chunk(CHUNK_TARGET_SECONDS)
            if chunk is not None:
                run.live.submit(chunk)
        now = time.monotonic()
        width, rows = _term_size()
        status = run.live.status if run.live is not None else ""
        redraw(
            session, net=net,
            transient=run.typewriter.visible(run.text(), now),
            draft=run.draft(),
            hint=_join_notes(hint, status),
            hud=_hud_lines(recorder, run, started, width, _hud_budget(rows), now),
            soft=True, now=now,
        )


def _drain_live(session, net, live, prefix="", typewriter=None):
    """等分段转写全部收尾，已识别文本持续上屏。

    返回是否被中断（Ctrl+C / 退出信号）：中断时保留已识别部分、丢弃未处理段
    （原始录音已在磁盘，不丢内容）。
    """
    interrupted = False
    tw = typewriter or fx.Typewriter()
    t0 = time.monotonic()
    try:
        while not _QUIT_REQUESTED:
            done, total = live.done_total()
            if done >= total and not live.status:
                break
            now = time.monotonic()
            hint = _wait_hint("转写收尾中", t0, now, extra=f"{done}/{total} 段")
            if live.status:
                hint = f"{fx.paint(live.status, fx.MUTED)}\n{hint}"
            redraw(session, net=net, transient=tw.visible(prefix + live.text(), now),
                   hint=hint, soft=True, now=now)
            _poll_key(FRAME_SECONDS)  # 顺带吞掉等待期间的按键
        else:
            interrupted = True
    except KeyboardInterrupt:
        interrupted = True
    live.finish(abandon_pending=interrupted)
    return interrupted


def _finish_streaming(session, engine, cloud, audio, wav, note):
    """Paraformer 流式路径的收尾：取流式结果；失败用已录 buffer 本地兜底。"""
    net = engine.net
    if audio is None or audio.size == 0:
        cloud.finalize()
        return None, note, wav
    text = ""
    try:
        text = _await_animated(session, net, "转写中", cloud.finalize)
        if cloud.failed or not text:
            if not net.probe():
                net.go_offline()
                note = _join_notes(note, "▲ 网络中断，已转为离线转写")
            text = _local_transcribe(session, engine, audio)
    except KeyboardInterrupt:
        note = _join_notes(note, "▲ 已中断")
    if not text and wav:
        wav = mark_keep(wav)
        note = _join_notes(note, f"原始录音已保留：{wav}")
    return text, note, wav


def _capture(session, engine, recording_hint, memory_store=None):
    """录音并转写。返回 (text_or_None, note, wav_path)。

    崩溃安全契约：
    - 录音全程由独立线程写磁盘流水账，stop() 时原子转 WAV；任何退出路径都
      先停录保文件（临界区内，不被信号打断），再谈别的；
    - 实时路径边说边出字；实时会话失败即热切换到分段（定稿不丢不重复）；
      分段路径按段转写，单段失败只影响该段；
    - 失败 / 中断 / 异常时 WAV 标记为保留件（不被清理）并回显路径；
    - 本函数不向外抛异常（常规异常记日志、转为返回值；Ctrl+C 录音中 = 停止，
      收尾中 = 保留已识别部分）。

    全部转写完成后才返回文本，由调用方决定是否进入 AI 流程。
    text 为 None 表示没录到音频，"" 表示录到了但没识别出文字。
    """
    global _SCREEN_BUSY
    net = engine.net
    recorder = engine.recorder
    state = {"audio": None, "tail": None, "wav": None, "stopped": False}

    def _ensure_stopped():
        """无论从哪条路退出，先停录保 WAV——顺序即优先级；临界区不被信号打断。"""
        if state["stopped"]:
            return
        state["stopped"] = True
        try:
            with _defer_signals():
                state["audio"], state["tail"], state["wav"] = recorder.stop()
        except KeyboardInterrupt:
            pass  # 停录本身就是 Ctrl+C 想要的；退出意图已记入 _QUIT_REQUESTED
        except Exception:
            errlog.log_exception("停止录音失败")

    note = ""
    if not net.is_online() and net.probe():
        net.go_online()
        note = "✓ 网络已恢复，切回在线"

    context = _build_capture_context(session, memory_store)
    run = _CaptureRun(engine, context)
    _SCREEN_BUSY = True
    try:
        use_cloud = net.is_online() and engine.cloud_available
        if use_cloud and engine.online_engine == "paraformer":
            try:
                run.cloud = engine.get_cloud()
                run.cloud.begin()
            except Exception:
                errlog.log_exception("Paraformer 建立会话失败")
                if not net.probe():
                    net.go_offline()
                    note = "▲ 网络不可用，已进入离线模式"
                run.cloud = None
        elif use_cloud and engine.online_engine == "qwen-realtime":
            redraw(session, net=net,
                   hint=f"{fx.spinner(time.monotonic())} "
                        f"{fx.paint('连接实时识别…', fx.MUTED)}")
            try:
                run.rt = engine.get_realtime()
                run.rt.begin(context)
            except Exception:
                errlog.log_exception("实时识别建连失败")
                if not net.probe():
                    net.go_offline()
                    note = "▲ 网络不可用，已进入离线模式"
                else:
                    note = "▲ 实时识别不可用，本次改用分段转写"
                run.rt = None
        # 分段调度器：Qwen 分段引擎 / 离线 / 上面两条路起步失败
        if run.cloud is None and run.rt is None:
            run.live = LiveTranscriber(engine, context)

        redraw(session, net=net, hint=recording_hint)
        on_frame = run.rt.feed if run.rt is not None else (run.cloud.feed if run.cloud is not None else None)
        recorder.start(on_frame=on_frame)
        try:
            with _cbreak():
                _record_loop(session, engine, net, recording_hint, run)
        except KeyboardInterrupt:
            pass  # Ctrl+C / 退出信号 = 停止录音
        finally:
            _ensure_stopped()

        audio, tail, wav = state["audio"], state["tail"], state["wav"]

        if run.cloud is not None:
            return _finish_streaming(session, engine, run.cloud, audio, wav, note)

        if audio is None or audio.size == 0:
            if run.rt is not None:
                run.rt.close()
            if run.live is not None:
                run.live.finish(abandon_pending=True)
            return None, note, wav

        interrupted = False
        # ── 实时路径收尾：等服务端补完最后一句 ──
        if run.rt is not None and run.live is None:
            rt = run.rt
            try:
                _await_animated(
                    session, net, "收尾中",
                    lambda: rt.finalize(timeout=REALTIME_FINISH_TIMEOUT),
                    transient_fn=lambda: run.typewriter.visible(rt.final_text(), time.monotonic()),
                    draft_fn=rt.preview_text,
                )
            except KeyboardInterrupt:
                interrupted = True
                rt.close()
            if rt.failed and not interrupted:
                run.fallback_for_rest(audio, recorder.samplerate)
        elif run.rt is not None and run.live is not None:
            run.rt.close()  # 录音期间已热切换：实时会话早已失效
            if tail is not None and tail.size:
                run.live.submit(tail)
        elif run.live is not None and tail is not None and tail.size:
            run.live.submit(tail)

        # ── 分段调度器收尾（分段路径 / 实时失败的接手者）──
        failed = 0
        if run.live is not None and not interrupted:
            try:
                prefix = run.rt.final_text() if run.rt is not None else ""
                with _cbreak():
                    interrupted = _drain_live(session, net, run.live, prefix=prefix,
                                              typewriter=run.typewriter)
            except KeyboardInterrupt:
                interrupted = True
                run.live.finish(abandon_pending=True)
            failed = run.live.failed_count()
        elif run.live is not None:
            run.live.finish(abandon_pending=True)

        text = run.text()
        extras = []
        if run.failover_note:
            extras.append(run.failover_note)
        if run.live is not None:
            if run.live.fell_offline:
                extras.append("▲ 网络中断，已切本地转写")
            elif run.live.cloud_errors:
                extras.append(f"▲ 云端出错，{run.live.cloud_errors} 段改用本地转写")
        if failed:
            extras.append(f"▲ {failed} 段识别失败")
        if interrupted:
            extras.append("▲ 已中断，保留中断前识别的部分")
        if (failed or interrupted or run.failover_note) and wav:
            wav = mark_keep(wav)
            extras.append(f"原始录音已保留：{wav}")
        return text, _join_notes(note, *extras), wav
    except Exception as e:
        _ensure_stopped()
        for closer in ((run.rt.close if run.rt is not None else None),
                       ((lambda: run.live.finish(abandon_pending=True)) if run.live is not None else None)):
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
        log_path = errlog.log_exception("录音/转写环节异常")
        wav = mark_keep(state["wav"]) if state["wav"] else None
        msg = f"✕ 出错（{e}），原始录音已保留：{wav}" if wav else f"✕ 出错：{e}"
        if log_path:
            msg += f" · 日志 {log_path}"
        return None, msg, wav
    finally:
        _SCREEN_BUSY = False


# ── 提交后的小动画 ────────────────────────────────────

def _celebrate(session, net, hint, added=""):
    """提交成功：新增文本逐字揭示 + 提示行掠光两拍，末尾留一只满足的猫。"""
    global _SCREEN_BUSY
    if not (_is_tty() and fx.ENABLED):
        redraw(session, net=net, hint=hint)
        return
    _SCREEN_BUSY = True
    try:
        content = session.content
        base = content[: len(content) - len(added)] if added and content.endswith(added) else content
        reveal = content[len(base):]
        frames = 8
        for i in range(1, frames + 1):
            t = i / frames
            shown = base + reveal[: int(len(reveal) * t)]
            redraw(session, net=net, hint=fx.sweep(hint, i * 0.12, speed=1.0),
                   soft=True, content=shown)
            time.sleep(0.04)
    finally:
        _SCREEN_BUSY = False
    redraw(session, net=net, hint=hint)


# ── 命令：写入 / AI ──────────────────────────────────

def cmd_write(session, engine, ai_client, memory_store=None, strong=False):
    """写入模式：录音 → 流式/分段转写 →（在线时）AI 润色 → 仅把本次新话追加到末尾。

    全部转写完成后才进入 AI 流程（strong=False 忠实清理，strong=True 升华）。
    AI 失败/中断时写入原始转写并提示——内容优先于修饰。离线时跳过 AI，直接
    追加原始转写（纯转写模式）。提交与落盘在临界区内完成。
    """
    net = engine.net
    raw_text, note, _wav = _capture(
        session, engine, "● 录音中… 按回车停止", memory_store=memory_store
    )
    if raw_text is None:
        redraw(session, net=net, hint=note or "▲ 未检测到音频")
        return
    if not raw_text:
        redraw(session, net=net, hint=note or "▲ 未识别到文字")
        return

    text = raw_text
    if ai_client is not None and net.is_online() and not _QUIT_REQUESTED:
        label = "升华" if strong else "润色"
        polish = ai_client.polish_text if strong else ai_client.polish_text_light
        try:
            text = _await_animated(session, net, f"{label}中",
                                   lambda: polish(raw_text),
                                   transient_fn=lambda: raw_text) or raw_text
        except KeyboardInterrupt:
            note = _join_notes(note, f"▲ {label}已中断，写入原始转写")
        except Exception:
            errlog.log_exception(f"AI {label}失败")
            if not net.probe():
                net.go_offline()
                note = _join_notes(note, "▲ 网络中断，写入原始转写")
            else:
                note = _join_notes(note, f"▲ {label}失败，写入原始转写")

    new_content = session.content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += text
    with _defer_signals():
        session.commit(new_content)
        copy_to_clipboard(session.content)
    if not net.is_online():
        label = "已转写(离线)"
    else:
        label = "升华" if strong else "已写入"
    _celebrate(session, net, _join_notes(f"✓ {label} ({len(session.content)}字)", note), added=text)


def cmd_ai(session, engine, ai_client, memory_store=None):
    """AI 指令模式：录音为指令 → AI 处理 → 覆盖内容，压入历史（仅在线）。"""
    net = engine.net
    instruction, note, _wav = _capture(
        session, engine, "● 说出指令… 按回车停止", memory_store=memory_store
    )
    if instruction is None:
        redraw(session, net=net, hint=note or "▲ 未检测到音频")
        return
    if not instruction:
        redraw(session, net=net, hint=note or "▲ 未识别到文字")
        return
    # 录音期间可能掉线 → AI 不可用
    if not net.is_online():
        redraw(session, net=net, hint="▲ 网络不可用，AI 指令已取消（离线）")
        return
    if _QUIT_REQUESTED:
        redraw(session, net=net, hint="▲ 正在退出，AI 指令已取消")
        return

    try:
        result = _await_animated(session, net, "AI 处理中",
                                 lambda: ai_client.process_document(session.content, instruction),
                                 transient_fn=lambda: f"{instruction}")
    except KeyboardInterrupt:
        redraw(session, net=net, hint="▲ AI 已中断，内容未变")
        return
    except Exception as e:
        errlog.log_exception("AI 指令失败")
        if not net.probe():
            net.go_offline()
            redraw(session, net=net, hint="▲ 网络中断，已转离线，AI 指令取消")
        else:
            redraw(session, net=net, hint=f"✕ {e}")
        return
    with _defer_signals():
        session.commit(result)
        copy_to_clipboard(session.content)
    _celebrate(session, net, "✓ AI 已覆盖", added=session.content)


# ── 命令：编辑 ───────────────────────────────────────

def _edit_dir():
    d = os.path.join(STATE_DIR, "edit")
    os.makedirs(d, exist_ok=True)
    return d


def _prune_edit_dir(keep=20):
    """保留最近 keep 份草稿，多余的按 mtime 从旧到新清理。"""
    d = _edit_dir()
    try:
        entries = sorted(
            (os.path.join(d, n) for n in os.listdir(d) if n.startswith("edit-")),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
    except OSError:
        return
    for p in entries[keep:]:
        try:
            os.unlink(p)
        except OSError:
            pass


def cmd_compose(session):
    """编辑模式：用 $EDITOR 直接编辑当前内容，保存退出后提交到历史。

    草稿写入状态目录，仅在 commit 成功或确认无变更后删除；
    异常退出时保留文件以便恢复。
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"

    _prune_edit_dir()
    edit_dir = _edit_dir()
    fname = f"edit-{int(time.time() * 1000)}-{os.getpid()}.md"
    path = os.path.join(edit_dir, fname)

    with open(path, "w", encoding="utf-8") as f:
        f.write(session.content)

    try:
        cmd = shlex.split(editor) + [path]
        rc = subprocess.run(cmd).returncode
    except FileNotFoundError:
        try:
            os.unlink(path)
        except OSError:
            pass
        redraw(session, hint=f"✕ 找不到编辑器：{editor}")
        return

    if rc != 0:
        redraw(session, hint=f"▲ 编辑器异常退出 (rc={rc})  草稿保留：{path}")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            new_content = f.read()
    except OSError as e:
        redraw(session, hint=f"✕ 读取草稿失败：{e}  路径：{path}")
        return

    # 编辑器通常会在末尾自动追加一个换行,去掉再做对比,避免误判为有变更
    if new_content.endswith("\n"):
        new_content = new_content[:-1]

    if new_content == session.content:
        try:
            os.unlink(path)
        except OSError:
            pass
        redraw(session, hint="· 内容未变")
        return

    with _defer_signals():
        session.commit(new_content)
        copy_to_clipboard(session.content)
    try:
        os.unlink(path)
    except OSError:
        pass
    redraw(session, hint=f"✓ 已编辑 ({len(session.content)}字)")


# ── 命令：撤销 / 重做 / 清空 / 复制 ──────────────────

def cmd_undo(session):
    if session.undo():
        redraw(session, hint="已撤销")
    else:
        redraw(session, hint="▲ 没有更早版本")


def cmd_redo(session):
    if session.redo():
        redraw(session, hint="已重做")
    else:
        redraw(session, hint="▲ 没有更新版本")


def _flash_memory_saved():
    """记忆后台更新完成时由更新线程回调：在当前行原地闪烁“· 记忆更新”两下约 1 秒。

    主循环此刻通常阻塞在按键等待（不向屏幕输出），只有本线程在写；若主线程正在
    录音/收尾/屏保等高频重绘（_SCREEN_BUSY），则跳过闪烁，避免抢屏。
    """
    if not _is_tty() or _SCREEN_BUSY:
        return
    msg = fx.paint("· 记忆更新", fx.MUTED)
    try:
        for _ in range(2):
            sys.stdout.write("\r\033[2K" + msg)
            sys.stdout.flush()
            time.sleep(0.25)
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
            time.sleep(0.2)
    except Exception:
        pass


def _maybe_update_memory(memory_store, session, ai_client, net):
    """一次 AI 功能后调用：累计满 N 次则后台更新长期记忆（条件 A）。

    需在线且 AI 可用（记忆更新本身是 LLM 调用）。离线时不计数、不更新。
    """
    if memory_store is None or ai_client is None or not net.is_online():
        return
    if memory_store.bump_counter():
        memory_store.update_async(
            session.content, ai_client, on_done=_flash_memory_saved, force=False
        )


def cmd_clear(session, memory_store=None, ai_client=None, net=None):
    """清空当前内容，压入历史（可撤销）。

    [D] 是用户“任务切换”的天然快照点（条件 B）：清空前先把当前文稿快照交给记忆
    做一次更新存档（先抓取文稿 → 后台更新记忆 → 再清空），避免任务内容随清空而
    流失。记忆线程拿的是文稿副本，故立即清空不影响其归档；且清空本身可 [Z] 撤销。
    """
    doc = session.content
    if (
        memory_store is not None
        and ai_client is not None
        and net is not None
        and net.is_online()
        and doc.strip()
    ):
        memory_store.update_async(
            doc, ai_client, on_done=_flash_memory_saved, force=True
        )
        memory_store.reset_counter()
    session.commit("")
    redraw(session, hint="已清空")


def cmd_copy(session):
    if not session.content:
        redraw(session, hint="▲ 内容为空")
        return
    copy_to_clipboard(session.content)
    redraw(session, hint="已复制")


# ── 记忆模式 ─────────────────────────────────────────

def _redraw_memory(memory_store, hint="", input_buffer=None, input_prompt=""):
    """渲染长期记忆界面：今天的记忆 + 最近几天，底部菜单/确认区。"""
    _clear()
    w = _term_width()
    today = memory_store.today_items()
    recent = memory_store.recent_days(exclude_today=True)

    print(fx.rule(w))
    today_chars = sum(len(x) for x in today)
    print(f"{fx.paint('长期记忆', fx.HI, bold=True)}"
          f"{fx.paint(f' · 今天（{today_chars} 字）', fx.MUTED)}")
    if not today:
        print(fx.paint("  (今天还没有记忆)", fx.MUTED))
    else:
        for it in today:
            for line in _wrap_text("• " + it, w):
                print(line)
    if recent:
        print()
        print(fx.paint("最近几天（仅供生成参考，喂给识别的只有今天这条）：", fx.MUTED))
        for d in recent:
            head = f"  {d['date']}："
            body = "；".join(d["items"]) if d["items"] else "（空）"
            for line in _wrap_text(head + body, w):
                print(line)
    print(fx.rule(w))

    if input_buffer is not None:
        avail = max(1, w - len(input_prompt) - 1)
        visible = input_buffer[-avail:] if len(input_buffer) > avail else input_buffer
        print(f"{input_prompt}{visible}▌")
        print(fx.rule(w))
        print("↵ 确认 | ESC 取消")
    else:
        print("[E]编辑今天  [D]清空全部  [ESC]返回")

    if hint:
        print(hint)


def _edit_memory_today(memory_store):
    """用 $EDITOR 编辑今天的记忆（每行一条），保存后写回。返回提示串。"""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    edit_dir = _edit_dir()
    path = os.path.join(edit_dir, f"memory-{int(time.time() * 1000)}-{os.getpid()}.txt")
    header = "# 每行一条记忆，空行忽略；以 # 开头的行为注释会被忽略。\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(memory_store.today_items()))

    try:
        rc = subprocess.run(shlex.split(editor) + [path]).returncode
    except FileNotFoundError:
        return f"✕ 找不到编辑器：{editor}"
    if rc != 0:
        return f"▲ 编辑器异常退出 (rc={rc})  草稿保留：{path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return f"✕ 读取草稿失败：{e}  路径：{path}"

    items = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    memory_store.set_today(items)
    try:
        os.unlink(path)
    except OSError:
        pass
    return f"✓ 已更新今天的记忆（{len(items)} 条）"


def cmd_memory(session, memory_store):
    """[M] 记忆模式：查看 / 编辑今天 / 清空全部。按 ESC 返回主界面。"""
    hint = ""
    while True:
        _redraw_memory(memory_store, hint=hint)
        hint = ""

        if not _is_tty():
            break
        try:
            ch = _getch_raw()
        except (EOFError, KeyboardInterrupt):
            break

        if ch in ("\x1b", ""):  # 单独 ESC / EOF 返回
            break
        if ch.startswith("\x1b"):  # 方向键等忽略
            continue

        choice = ch.lower()
        if choice == "e":
            hint = _edit_memory_today(memory_store)
        elif choice == "d":
            # 清空前确认；clear() 内部会先写 .bak 存档（优先可恢复）
            _redraw_memory(memory_store, input_buffer="", input_prompt="▲ 清空全部记忆？输入 y 确认：")
            try:
                confirm = _getch_raw()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm.lower() == "y":
                memory_store.clear()
                hint = "已清空长期记忆（旧记忆已存档 .bak）"
            else:
                hint = "▲ 已取消"
        elif choice in ("q", "m"):
            break

    redraw(session)


def _guard_ai(session, net, feature):
    """AI 功能入口守卫。返回 True 表示当前在线、可继续。

    离线时探测是否恢复：恢复则切回在线并提示重试；仍断网则提示该功能离线不可用。
    两种情况都返回 False（恢复后让用户再按一次，操作栏也会先刷新出 AI 键）。
    """
    if net.is_online():
        return True
    if net.probe():
        net.go_online()
        redraw(session, net=net, hint=f"✓ 网络已恢复，请重试{feature}")
    else:
        redraw(session, net=net, hint=f"▲ 离线模式不支持{feature}")
    return False


# 启动画面上的那只猫：不乱跑，就在字标旁边坐着做动作
_SPLASH_CAT = fx.Cat(24, roam=False)


# ── 启动画面 / 菜单等待 / 屏保 ────────────────────────

def _boot(net, notes):
    """启动画面：品牌流光 + 启动日志，网络探测在后台线程并行（动画不拖慢启动）。

    返回探测结果（是否在线）。非 TTY 或关闭特效时退化为一行提示。
    """
    result = {}
    t = threading.Thread(target=lambda: result.update(online=net.probe()),
                         name="boot-probe", daemon=True)
    t.start()
    if not (_is_tty() and fx.ENABLED):
        print("· 检测网络…")
        t.join()
        return result.get("online", False)
    t0 = time.monotonic()
    try:
        while t.is_alive() or time.monotonic() - t0 < SPLASH_MIN_SECONDS:
            now = time.monotonic()
            w, _ = _term_size()
            lines = [""]
            _SPLASH_CAT.update(now)
            lines.extend("   " + fx.paint(r, fx.FAINT) for r in _SPLASH_CAT.render(24, now))
            lines += ["", " " + fx.banner(w - 2, now),
                      " " + fx.paint("crash-safe voice \u2192 text", fx.FAINT, italic=True), ""]
            for n in notes:
                lines.append(" " + fx.paint("\u25b8 ", fx.FAINT) + fx.paint(n, fx.MUTED))
            online_word = "在线" if result.get("online") else "离线"
            probe_line = (f" {fx.spinner(now)} {fx.paint('检测网络…', fx.MUTED)}" if t.is_alive()
                          else f" {fx.paint('\u2713', fx.INK)} {fx.paint('网络 ' + online_word, fx.MUTED)}")
            lines.append(probe_line)
            lines.append(" " + fx.rule(w - 2, now, active=True))
            _write_frame(lines, soft=True)
            t.join(FRAME_SECONDS)
    except KeyboardInterrupt:
        pass
    t.join()
    return result.get("online", False)


def _screensaver_status(width, elapsed, now):
    """屏保底部的一行状态。放不下就依次丢字标、丢文案——绝不折行：
    折行会多占一行、把雨顶出屏幕，整屏就滚起来了。"""
    idle = f"空闲 {fx.elapsed_text(elapsed)} · 按任意键唤醒"
    for cand in (f" {fx.banner(min(width, 24), now)}  {fx.paint(idle, fx.FAINT)}",
                 f" {fx.paint(idle, fx.FAINT)}",
                 f" {fx.paint(fx.elapsed_text(elapsed), fx.FAINT)}"):
        if fx.display_width(cand) <= width:
            return cand
    return ""


def _screensaver(session, net):
    """空闲屏保：猫之矩阵——数字雨里有只散步的猫，暗处偶尔亮起一对眼睛。

    任意键唤醒（该键被吞掉，不作为命令）。"""
    global _SCREEN_BUSY
    _SCREEN_BUSY = True
    try:
        w, rows = _term_size()
        rain = fx.MatrixRain(w, max(1, rows - 1))
        t0 = time.monotonic()
        while not _QUIT_REQUESTED:
            w, rows = _term_size()
            rain.resize(w, max(1, rows - 1))
            now = time.monotonic()
            rain.step(now)
            lines = rain.render(now)
            status = _screensaver_status(w, now - t0, now)
            lines.append(status)
            _write_frame(lines, soft=True)
            if _poll_key(FRAME_SECONDS) is not None:
                return
    finally:
        _SCREEN_BUSY = False


def _menu_key(session, net):
    """主界面等待按键。空闲超过 FX_IDLE_SECONDS 进入屏保；唤醒后返回 None（调用方重绘）。"""
    if not _is_tty():
        return _getch()
    idle = FX_IDLE_SECONDS if fx.ENABLED else 0
    waited = 0.0
    with _cbreak():
        while not _QUIT_REQUESTED:
            k = _poll_key(0.25)
            if k is not None:
                return k
            waited += 0.25
            if idle and waited >= idle:
                _screensaver(session, net)
                return None
    return ""


# ── 启动与主循环 ─────────────────────────────────────

def main():
    global _NET
    errlog.setup_hooks()
    _install_signal_handlers()
    notes = []  # 启动信息汇总到第一屏的提示区，避免被清屏一闪而过

    # ── 恢复上次会话：屏幕空白、上次内容留在历史里，[Z] 一步找回 ──
    session = Session()
    if session.load_warning:
        notes.append(f"▲ {session.load_warning}")
    last_saved = session.saved_at
    prev_chars = session.start_fresh()
    if prev_chars:
        when = f"，上次保存 {last_saved}" if last_saved else ""
        notes.append(f"· 上次内容（{prev_chars} 字{when}）已存入历史，按 [Z] 找回")

    # ── 恢复上次崩溃遗留的录音流水账 ──
    for path, secs in recover_orphan_pcms():
        notes.append(f"· 上次未完成的录音已找回（{secs:.0f} 秒）：{path}")
    prune_wavs()

    from synclisten.core.memory import MemoryStore
    memory_store = MemoryStore()  # 持久化、跨重启、全局一份

    net = NetworkManager()
    _NET = net
    recorder = AudioRecorder()

    if _boot(net, notes):
        net.go_online()
    else:
        net.go_offline()

    from synclisten.config import DASHSCOPE_API_KEY
    cloud_available = bool(DASHSCOPE_API_KEY)
    engine = TranscribeEngine(recorder, net, cloud_available)

    ai_client = None
    if net.is_online():
        try:
            ai_client = AIClient()
        except ValueError as e:
            notes.append(f"▲ AI 未配置: {e}（[S]/[A] 不可用）")
        if cloud_available:
            if engine.online_engine == "paraformer":
                notes.append("✓ 在线模式就绪（Paraformer 云端流式转写，启动未加载本地模型）")
            elif engine.online_engine == "qwen":
                notes.append(
                    f"✓ 在线模式就绪（Qwen3-ASR-Flash 分段云端转写，约 {CHUNK_TARGET_SECONDS:g}s 一段"
                    " + 上下文注入，启动未加载本地模型）"
                )
            else:
                notes.append(
                    "✓ 在线模式就绪（Qwen3-ASR-Flash-Realtime 流式转写 · 边说边出字 + 上下文注入，"
                    "启动未加载本地模型）"
                )
        else:
            notes.append("▲ 未配置 DASHSCOPE_API_KEY：语音转写将使用本地 SenseVoice（首次约 5s 加载）")
    else:
        notes.append("○ 网络不可用，进入离线模式（本地 SenseVoice 转写，AI 已停用）")

    redraw(session, net=net, hint="\n".join(notes))

    try:
        while not _QUIT_REQUESTED:
            try:
                ch = _menu_key(session, net)
            except (EOFError, KeyboardInterrupt):
                break
            if ch is None:  # 屏保唤醒：重绘后继续等
                redraw(session, net=net)
                continue
            if ch == "":  # 非交互输入 EOF
                break

            # 主界面下 ESC 与方向键等转义序列：忽略
            if ch.startswith("\x1b"):
                continue

            choice = ch.lower()

            # 每个命令都在防护内执行：出错显示提示并回到主界面，绝不退出
            try:
                if choice == "\r" or choice == "\n":
                    cmd_write(session, engine, ai_client, memory_store)
                    _maybe_update_memory(memory_store, session, ai_client, net)
                elif choice == "s":
                    if not _guard_ai(session, net, "升华"):
                        pass
                    elif ai_client is None:
                        redraw(session, net=net, hint="▲ AI 未配置")
                    else:
                        cmd_write(
                            session, engine, ai_client, memory_store, strong=True
                        )
                        _maybe_update_memory(memory_store, session, ai_client, net)
                elif choice == "e":
                    cmd_compose(session)
                elif choice == "a":
                    if not _guard_ai(session, net, "AI 指令"):
                        pass
                    elif ai_client is None:
                        redraw(session, net=net, hint="▲ AI 未配置")
                    else:
                        cmd_ai(session, engine, ai_client, memory_store)
                        _maybe_update_memory(memory_store, session, ai_client, net)
                elif choice == "m":
                    cmd_memory(session, memory_store)
                elif choice == "z":
                    cmd_undo(session)
                elif choice == "x":
                    cmd_redo(session)
                elif choice == "d":
                    cmd_clear(session, memory_store, ai_client, net)
                elif choice == "c":
                    cmd_copy(session)
                elif choice == "q":
                    break
                else:
                    redraw(session, net=net, hint=f"? 未知按键: {repr(ch)}")
            except Exception as e:
                log_path = errlog.log_exception(f"处理按键 {choice!r} 时异常")
                msg = f"✕ 操作失败（已回到主界面）：{e}"
                if log_path:
                    msg += f" · 日志 {log_path}"
                redraw(session, net=net, hint=msg)
            _flush_input()
    except KeyboardInterrupt:
        pass  # Ctrl+C / SIGTERM / SIGHUP：走下方统一退出清理

    # ── 退出清理：录音兜底停录（保 WAV）、记忆线程落盘 ──
    try:
        if recorder.stream is not None:
            try:
                recorder.stop()
            except Exception:
                errlog.log_exception("退出时停止录音失败")
        if memory_store.wait_pending(timeout=0.0):
            redraw(session, net=net, hint="· 正在保存记忆…")
            memory_store.wait_pending(timeout=10.0)
    except KeyboardInterrupt:
        pass  # 再按一次 Ctrl+C：不等了，会话早已随每次操作落盘
    _clear()
    if session.save_error:
        print(f"▲ 会话落盘失败（{session.save_error}），内容可能未保存：{session.path}")
    else:
        print(f"· 会话已保存（{len(session.content)} 字），下次启动按 [Z] 找回：{session.path}")


if __name__ == "__main__":
    main()
