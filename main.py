#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SyncListen — 极简语音工作流
一次运行 = 一个会话，退出不保存

模式：
  在线模式 — 云端 Paraformer 流式转写（边录边传）+ AI 全功能；启动不加载本地模型，最快。
  离线模式 — 网络不可用时自动回退：本地 SenseVoice 转写，AI 功能停用、相关快捷键隐藏。
  切换：启动探测一次；在线操作失败即探测确认是否断网；离线时每次操作探测是否恢复，自动来回切换。

[回车] 写入模式  — 录音转写，忠实清理（去口癖语病、保留原话）后追加（离线时直接追加原始转写）
[S]    升华写入  — 录音转写，AI 深度润色（口语转书面、精炼有逻辑）后追加（仅在线）
[E]    编辑模式  — 用 $EDITOR 直接编辑当前内容
[A]    AI 指令   — 语音指令，AI处理当前内容（仅在线）
[F]    词语修复  — AI 扫描全文修复语音转写错词（支持补充参考词与替换配对）（仅在线）
[T]    易错词    — 管理持久化易错词表（增/删/查）
[D]    清空      — 清空当前内容（可撤销）
[Z]    撤销      — 回滚到上一版本
[X]    重做      — 前进到下一版本
[C]    复制      — 手动复制到剪贴板
[Q]    退出

易错词通过 [T] 管理后会持久化保存，作为 [F] 词语修复的参考词表
"""

import os
import shlex
import shutil
import sys
import subprocess
import textwrap
import time

from synclisten.core.recorder import AudioRecorder
from synclisten.core.ai_client import AIClient
from synclisten.core.terminology import TerminologyStore
from synclisten.core.network import NetworkManager
# 注意：SenseVoiceTranscriber / CloudTranscriber 均按需延迟导入与实例化，
# 见 TranscribeEngine——在线模式下不加载本地模型，启动更快。

# 当前网络管理器（在 main() 中设置）。redraw 在未显式传 net 时回退到它，
# 以便所有界面（撤销/复制/编辑等）离线时都能保持离线标记一致。
_NET = None

# ── 跨平台即时按键读取 ──────────────────────────────────
def _is_tty():
    return sys.stdin.isatty()


if sys.platform == "win32":
    import msvcrt

    def _getch_raw():
        """读取单个按键。

        - 普通字符返回单字符;
        - 单独的 ESC 返回 "\\x1b"(长度 1);
        - 方向键/功能键返回以 "\\x1b" 开头的多字符串(调用方据此区分并忽略)。
        """
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            # 功能键/方向键:再读一个字节,整体当作转义序列返回
            nxt = msvcrt.getch()
            return "\x1b[" + nxt.decode("latin-1", errors="ignore")
        return ch.decode("utf-8", errors="ignore")
else:
    import tty
    import termios
    import select

    def _getch_raw():
        """读取单个按键。

        - 普通字符返回单字符;
        - 单独的 ESC 返回 "\\x1b"(长度 1);
        - 方向键/功能键返回完整转义序列如 "\\x1b[A"(长度 > 1)。

        用 select 以极短超时区分「单独的 ESC」与「转义序列」,
        避免读到单独 ESC 时阻塞等待下一次按键。
        """
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                seq = ch
                # 在 cbreak 模式下探测后续字节;单独 ESC 时无后续,循环立即结束
                while select.select([fd], [], [], 0.05)[0]:
                    seq += sys.stdin.read(1)
                    # CSI/SS3 序列以字母或 '~' 结尾
                    if seq[-1].isalpha() or seq[-1] == "~":
                        break
                return seq
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


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


def _wait_for_enter():
    """阻塞直到检测到回车/换行。"""
    if not _is_tty():
        try:
            input()
        except EOFError:
            pass
        return
    while True:
        ch = _getch_raw()
        if ch in ("\r", "\n"):
            break


class Session:
    """单会话状态（内存中，退出即消失）"""

    def __init__(self):
        self.content = ""
        self.history = [""]
        self.pos = 0

    def commit(self, new_content):
        """提交新版本，丢弃当前位置之后的旧版本。"""
        self.history = self.history[:self.pos + 1]
        self.history.append(new_content)
        self.pos += 1
        self.content = new_content

    def undo(self):
        if self.pos > 0:
            self.pos -= 1
            self.content = self.history[self.pos]
            return True
        return False

    def redo(self):
        if self.pos < len(self.history) - 1:
            self.pos += 1
            self.content = self.history[self.pos]
            return True
        return False


def _clear():
    print("\033[2J\033[H", end="")


def _term_width():
    """获取当前终端宽度。"""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _wrap_text(text, width):
    """将文本按终端宽度自动换行。"""
    if not text:
        return []
    lines = []
    for paragraph in text.split("\n"):
        if paragraph.strip() == "":
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            paragraph,
            width=width,
            break_long_words=True,
            replace_whitespace=True,
        )
        lines.extend(wrapped if wrapped else [""])
    return lines


def _fmt_content(text, width, max_lines=15):
    if not text:
        return ["✏️ ..."]
    lines = _wrap_text(text, width)
    if len(lines) > max_lines:
        lines = ["..."] + lines[-(max_lines - 1):]
    return lines


def redraw(session, transient="", hint="", input_buffer=None, net=None):
    """清屏并重绘分区：内容区 + 临时区 + 输入区 + 操作栏。

    Args:
        input_buffer: 如果为字符串（包括空串），则显示输入区，
                      None 则不显示输入区。
        net: 可选 NetworkManager。离线时顶部显示离线标记、底部操作栏隐藏 AI 键。
    """
    if net is None:
        net = _NET
    _clear()
    w = _term_width()

    # 离线标记：只有离线（异常态）才显示，在线保持干净
    if net is not None and not net.is_online():
        print("🔴 离线模式 · 本地转写 · AI 功能已停用".center(w))

    # 上半区：当前内容
    print("─" * w)
    for line in _fmt_content(session.content, w):
        print(line)
    print("─" * w)

    # 中间区：临时转写 / 状态提示
    if transient:
        wrapped = _wrap_text(transient, w - 3)  # 预留 🎙 前缀
        for i, line in enumerate(wrapped):
            prefix = "🎙 " if i == 0 else "   "
            print(f"{prefix}{line}")
        print("─" * w)

    # 输入区：用于收集专业名词
    if input_buffer is not None:
        prompt = "✏️ 专业名词："
        avail = w - len(prompt) - 1
        visible = input_buffer[-avail:] if len(input_buffer) > avail else input_buffer
        print(f"{prompt}{visible}▌")
        print("─" * w)
        hint = "↵ 确认 | ⌫ 删除 | ESC 跳过"

    # 底部操作栏：离线时隐藏依赖网络的 AI 键（[S] 升华 / [A] AI / [F] 修复）
    if net is not None and not net.is_online():
        print("[↵]✍️  [E]📝  [T]📒  [D]🗑  [Z]↩️  [X]↪️  [C]📋  [Q]👋")
    else:
        print("[↵]✍️  [S]✨  [E]📝  [A]🤖  [F]🔧  [T]📒  [D]🗑  [Z]↩️  [X]↪️  [C]📋  [Q]👋")
    if hint:
        print(hint)


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
        subprocess.run(
            ["wl-copy"],
            input=text.encode(),
            check=False,
        )
    elif ds == "x11":
        for sel in ("clipboard", "primary"):
            subprocess.run(
                ["xclip", "-selection", sel],
                input=text.encode(),
                check=False,
            )
    else:
        # 兜底：两个都试
        subprocess.run(
            ["wl-copy"],
            input=text.encode(),
            check=False,
        )
        for sel in ("clipboard", "primary"):
            subprocess.run(
                ["xclip", "-selection", sel],
                input=text.encode(),
                check=False,
            )


def _read_input_line(session, transient=""):
    """在 TTY 下实时读取一行用户输入（不回显到终端，由 redraw 渲染）。

    返回用户输入的字符串；按 ESC 取消返回空串。
    """
    buf = ""
    redraw(session, transient=transient, input_buffer=buf)

    while True:
        try:
            ch = _getch_raw()
        except (EOFError, KeyboardInterrupt):
            return ""

        # 回车确认
        if ch in ("\r", "\n"):
            return buf

        # 单独的 ESC 取消
        if ch == "\x1b":
            return ""

        # 方向键等转义序列：忽略
        if ch.startswith("\x1b"):
            continue

        # 退格 / DEL
        if ch in ("\x7f", "\b"):
            buf = buf[:-1]
            redraw(session, transient=transient, input_buffer=buf)
            continue

        # 可打印字符（排除控制字符）
        if len(ch) == 1 and ord(ch) >= 32:
            buf += ch
            redraw(session, transient=transient, input_buffer=buf)


class TranscribeEngine:
    """统一转写引擎：在线走云端流式 Paraformer，离线/失败回退本地 SenseVoice。

    本地模型懒加载——在线模式从不实例化它，保证启动最快；首次掉线才加载（约 5s）。
    云端识别器无本地模型，轻量，按需创建。
    """

    def __init__(self, recorder, net, cloud_available):
        self.recorder = recorder
        self.net = net
        self.cloud_available = cloud_available  # 是否配置了 DASHSCOPE_API_KEY
        self._local = None
        self._cloud = None

    def get_cloud(self):
        from synclisten.core.cloud_transcriber import CloudTranscriber
        if self._cloud is None:
            self._cloud = CloudTranscriber()
        return self._cloud

    def get_local(self):
        from synclisten.core.transcriber import SenseVoiceTranscriber
        if self._local is None:
            self._local = SenseVoiceTranscriber()
        return self._local


def _local_transcribe(session, engine, audio):
    """本地 SenseVoice 转写（首次会触发约 5s 模型加载，带自身提示）。"""
    redraw(session, net=engine.net, hint="🔄 本地转写中…")
    local = engine.get_local()
    text, _ = local.transcribe(audio)
    return text


def _capture(session, engine, recording_hint):
    """录音并转写，返回 (text_or_None, note)。

    在线：Paraformer 边录边传，停止即出结果；起步/中途断网则探测确认并回退本地。
    离线：先探测是否恢复；未恢复则本地 SenseVoice 兜底。
    过程中就地切换 engine.net 模式；note 携带模式切换提示（可空）。
    """
    net = engine.net
    recorder = engine.recorder
    note = ""

    # 离线态：每次操作探测是否恢复（“仅操作时检测”）
    if not net.is_online() and net.probe():
        net.go_online()
        note = "🟢 网络已恢复，切回在线"

    use_cloud = net.is_online() and engine.cloud_available
    cloud = None
    on_frame = None
    if use_cloud:
        try:
            cloud = engine.get_cloud()
            cloud.begin()
            on_frame = cloud.feed
        except Exception:
            # 起步即失败：探测确认是否真的断网
            if not net.probe():
                net.go_offline()
                note = "⚠️ 网络不可用，已进入离线模式"
            use_cloud = False
            cloud = None
            on_frame = None

    redraw(session, net=net, hint=recording_hint)
    recorder.start(on_frame=on_frame)
    _wait_for_enter()
    audio = recorder.stop()

    if audio is None or len(audio) == 0:
        if cloud is not None:
            cloud.finalize()
        return None, note

    # 在线流式路径
    if use_cloud and cloud is not None:
        redraw(session, net=net, hint="⏳ 转写中…")
        text = cloud.finalize()
        if cloud.failed or not text:
            # 云端中途失败：探测确认，用已录 buffer 本地兜底
            if not net.probe():
                net.go_offline()
                note = "⚠️ 网络中断，已转为离线转写"
            text = _local_transcribe(session, engine, audio)
        return text, note

    # 离线 / 无云端路径
    return _local_transcribe(session, engine, audio), note


def cmd_write(session, engine, ai_client, strong=False):
    """写入模式：录音 → 转写 →（在线时）AI 润色 → 仅把本次新话追加到末尾（不动旧文本）。

    Args:
        strong: False=忠实清理（去口癖语病，保留原话）；True=升华（口语转书面、精炼有逻辑）。
                两者都只作用于本次转写，整篇润色/改写请用 [A]，错词修复请用 [F]。
                离线时跳过 AI，直接追加原始转写（纯转写模式）。
    """
    net = engine.net
    raw_text, note = _capture(session, engine, "🔴 录音中… 按回车停止")
    if raw_text is None:
        redraw(session, net=net, hint=note or "⚠️ 未检测到音频")
        return
    if not raw_text:
        redraw(session, net=net, hint=note or "⚠️ 未识别到文字")
        return

    if ai_client is not None and net.is_online():
        if strong:
            redraw(session, net=net, transient=raw_text, hint="⏳ 升华中…")
            polish = ai_client.polish_text
        else:
            redraw(session, net=net, transient=raw_text, hint="⏳ 润色中…")
            polish = ai_client.polish_text_light
        try:
            text = polish(raw_text)
        except Exception:
            # AI 调用失败：探测确认是否断网，失败则保留原始转写
            if not net.probe():
                net.go_offline()
            text = raw_text
    else:
        text = raw_text

    new_content = session.content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += text
    session.commit(new_content)
    copy_to_clipboard(session.content)
    if not net.is_online():
        label = "已转写(离线)"
    else:
        label = "升华" if strong else "已写入"
    suffix = f"  ·  {note}" if note else ""
    redraw(session, net=net, hint=f"✅ {label} ({len(session.content)}字){suffix}")


def cmd_ai(session, engine, ai_client):
    """AI 指令模式：录音为指令 → AI 处理 → 覆盖内容，压入历史（仅在线）。"""
    net = engine.net
    instruction, note = _capture(session, engine, "🔴 说出指令… 按回车停止")
    if instruction is None:
        redraw(session, net=net, hint=note or "⚠️ 未检测到音频")
        return
    if not instruction:
        redraw(session, net=net, hint=note or "⚠️ 未识别到文字")
        return
    # 录音期间可能掉线 → AI 不可用
    if not net.is_online():
        redraw(session, net=net, hint="⚠️ 网络不可用，AI 指令已取消（离线）")
        return

    redraw(session, net=net, transient=f"📋 {instruction}", hint="⏳ AI 处理中…")
    try:
        result = ai_client.process_document(session.content, instruction)
        session.commit(result)
        copy_to_clipboard(session.content)
        redraw(session, net=net, hint="✅ AI 已覆盖")
    except Exception as e:
        if not net.probe():
            net.go_offline()
            redraw(session, net=net, hint="⚠️ 网络中断，已转离线，AI 指令取消")
        else:
            redraw(session, net=net, hint=f"❌ {e}")


def _edit_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(base, "synclisten", "edit")
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

    草稿写入 ~/.local/state/synclisten/edit/，仅在 commit 成功或确认无变更后删除；
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
        redraw(session, hint=f"❌ 找不到编辑器：{editor}")
        return

    if rc != 0:
        redraw(session, hint=f"⚠️ 编辑器异常退出 (rc={rc})  草稿保留：{path}")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            new_content = f.read()
    except OSError as e:
        redraw(session, hint=f"❌ 读取草稿失败：{e}  路径：{path}")
        return

    # 编辑器通常会在末尾自动追加一个换行,去掉再做对比,避免误判为有变更
    if new_content.endswith("\n"):
        new_content = new_content[:-1]

    if new_content == session.content:
        try:
            os.unlink(path)
        except OSError:
            pass
        redraw(session, hint="ℹ️ 内容未变")
        return

    session.commit(new_content)
    copy_to_clipboard(session.content)
    try:
        os.unlink(path)
    except OSError:
        pass
    redraw(session, hint=f"✅ 已编辑 ({len(session.content)}字)")


def cmd_repair(session, ai_client, terminology_store):
    """词语修复：AI 扫全文修复语音转写错词。支持补充参考词与替换配对。"""
    if not session.content:
        redraw(session, hint="⚠️ 内容为空")
        return

    raw = _read_input_line(
        session,
        transient="🔧 词语修复：用逗号分隔；单词为补充参考，错词=正确词为替换配对",
    )
    extra_terms, pairs = _parse_repair_input(raw)

    terms = list(terminology_store.list()) + extra_terms

    redraw(session, hint="⏳ 词语修复中…")
    try:
        result = ai_client.repair_words(
            session.content,
            terms=terms or None,
            pairs=pairs or None,
        )
    except Exception as e:
        redraw(session, hint=f"❌ {e}")
        return

    if not result or result == session.content:
        redraw(session, hint="ℹ️ 无需修复")
        return

    session.commit(result)
    copy_to_clipboard(session.content)
    redraw(session, hint=f"🔧 已修复 ({len(session.content)}字)")


def _parse_repair_input(raw):
    """解析 [F] 输入：逗号分隔，含 '=' 的 token 解析为 (错词, 正确词) 配对，其余为补充参考词。"""
    if not raw:
        return [], []
    terms = []
    pairs = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            wrong, _, correct = token.partition("=")
            wrong = wrong.strip()
            correct = correct.strip()
            if wrong and correct:
                pairs.append((wrong, correct))
        else:
            terms.append(token)
    return terms, pairs


def cmd_undo(session):
    if session.undo():
        redraw(session, hint="↩️ 已撤销")
    else:
        redraw(session, hint="⚠️ 没有更早版本")


def cmd_redo(session):
    if session.redo():
        redraw(session, hint="↪️ 已重做")
    else:
        redraw(session, hint="⚠️ 没有更新版本")


def cmd_clear(session):
    """清空当前内容，压入历史（可撤销）。"""
    session.commit("")
    redraw(session, hint="🗑 已清空")


def cmd_copy(session):
    if not session.content:
        redraw(session, hint="⚠️ 内容为空")
        return
    copy_to_clipboard(session.content)
    redraw(session, hint="📋 已复制")


def _redraw_terminology(store, hint="", input_buffer=None, input_prompt=""):
    """渲染易错词管理界面：列表 + 菜单/输入区。"""
    _clear()
    w = _term_width()
    terms = store.list()

    print("─" * w)
    print(f"📒 易错词表 (共 {len(terms)} 个)")
    print()
    if not terms:
        print("  (空)")
    else:
        # 编号位宽根据条目数量自适应
        idx_w = len(str(len(terms)))
        for i, term in enumerate(terms, 1):
            line = f"  {str(i).rjust(idx_w)}. {term}"
            # 超长词条按宽度截断显示
            if len(line) > w:
                line = line[: w - 1] + "…"
            print(line)
    print("─" * w)

    if input_buffer is not None:
        avail = max(1, w - len(input_prompt) - 1)
        visible = input_buffer[-avail:] if len(input_buffer) > avail else input_buffer
        print(f"{input_prompt}{visible}▌")
        print("─" * w)
        print("↵ 确认 | ⌫ 删除 | ESC 取消")
    else:
        print("[N]新增  [D]删除  [ESC]返回")

    if hint:
        print(hint)


def _read_terminology_input(store, prompt):
    """在易错词管理界面下读取一行输入。

    返回输入字符串；按 ESC 取消时返回 None；空串视为有效输入（由调用方处理）。
    """
    buf = ""
    _redraw_terminology(store, input_buffer=buf, input_prompt=prompt)

    while True:
        try:
            ch = _getch_raw()
        except (EOFError, KeyboardInterrupt):
            return None

        if ch in ("\r", "\n"):
            return buf

        # 单独的 ESC 取消
        if ch == "\x1b":
            return None

        # 方向键等转义序列：忽略
        if ch.startswith("\x1b"):
            continue

        if ch in ("\x7f", "\b"):
            buf = buf[:-1]
            _redraw_terminology(store, input_buffer=buf, input_prompt=prompt)
            continue

        if len(ch) == 1 and ord(ch) >= 32:
            buf += ch
            _redraw_terminology(store, input_buffer=buf, input_prompt=prompt)


def cmd_terminology(session, store):
    """易错词管理界面：列表 + 新增 / 删除。按 ESC 返回主界面。"""
    hint = ""
    while True:
        _redraw_terminology(store, hint=hint)
        hint = ""

        if not _is_tty():
            # 非交互环境直接返回，避免阻塞
            break

        try:
            ch = _getch_raw()
        except (EOFError, KeyboardInterrupt):
            break

        # 单独的 ESC 返回主界面
        if ch == "\x1b":
            break

        # 方向键等转义序列：忽略
        if ch.startswith("\x1b"):
            continue

        choice = ch.lower()

        if choice == "n":
            term = _read_terminology_input(store, "✏️ 新增易错词：")
            if term is None:
                hint = "⚠️ 已取消"
                continue
            term = term.strip()
            if not term:
                hint = "⚠️ 输入为空"
                continue
            if store.add(term):
                hint = f"✅ 已添加：{term}"
            else:
                hint = f"⚠️ 已存在：{term}"

        elif choice == "d":
            if not store.list():
                hint = "⚠️ 易错词表为空"
                continue
            raw = _read_terminology_input(store, "🗑 输入要删除的编号：")
            if raw is None:
                hint = "⚠️ 已取消"
                continue
            raw = raw.strip()
            try:
                idx = int(raw)
            except ValueError:
                hint = f"⚠️ 无效编号：{raw}"
                continue
            removed = store.remove(idx)
            if removed is None:
                hint = f"⚠️ 编号超出范围：{idx}"
            else:
                hint = f"✅ 已删除：{removed}"

        elif choice in ("q", "t"):
            break

        # 其他按键忽略，继续渲染

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
        redraw(session, net=net, hint=f"🟢 网络已恢复，请重试{feature}")
    else:
        redraw(session, net=net, hint=f"⚠️ 离线模式不支持{feature}")
    return False


def main():
    global _NET
    session = Session()
    terminology_store = TerminologyStore()

    net = NetworkManager()
    _NET = net
    recorder = AudioRecorder()

    print("🌐 检测网络…")
    if net.probe():
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
            print(f"⚠️  AI 未配置: {e}（[S]/[A]/[F] 不可用）")
        if cloud_available:
            print("✅ 在线模式就绪（Paraformer 云端流式转写，启动未加载本地模型）")
        else:
            print("⚠️  未配置 DASHSCOPE_API_KEY：语音转写将使用本地 SenseVoice（首次约 5s 加载）")
    else:
        print("🔴 网络不可用，进入离线模式（本地 SenseVoice 转写，AI 已停用）")

    time.sleep(0.5)
    redraw(session, net=net)

    while True:
        try:
            ch = _getch()
        except (EOFError, KeyboardInterrupt):
            _clear()
            break

        # 主界面下 ESC 与方向键等转义序列：忽略
        if ch.startswith("\x1b"):
            continue

        choice = ch.lower()

        if choice == "\r" or choice == "\n":
            cmd_write(session, engine, ai_client)
        elif choice == "s":
            if not _guard_ai(session, net, "升华"):
                pass
            elif ai_client is None:
                redraw(session, net=net, hint="⚠️ AI 未配置")
            else:
                cmd_write(session, engine, ai_client, strong=True)
        elif choice == "e":
            cmd_compose(session)
        elif choice == "a":
            if not _guard_ai(session, net, "AI 指令"):
                pass
            elif ai_client is None:
                redraw(session, net=net, hint="⚠️ AI 未配置")
            else:
                cmd_ai(session, engine, ai_client)
        elif choice == "f":
            if not _guard_ai(session, net, "词语修复"):
                pass
            elif ai_client is None:
                redraw(session, net=net, hint="⚠️ AI 未配置")
            else:
                cmd_repair(session, ai_client, terminology_store)
        elif choice == "t":
            cmd_terminology(session, terminology_store)
        elif choice == "z":
            cmd_undo(session)
        elif choice == "x":
            cmd_redo(session)
        elif choice == "d":
            cmd_clear(session)
        elif choice == "c":
            cmd_copy(session)
        elif choice == "q":
            _clear()
            break
        else:
            redraw(session, net=net, hint=f"? 未知按键: {repr(ch)}")


if __name__ == "__main__":
    main()
