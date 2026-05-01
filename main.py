#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SyncListen — 极简语音工作流
一次运行 = 一个会话，退出不保存

[回车] 写入模式  — 直接录音，AI润色后追加
[A]    AI 指令   — 语音指令，AI处理当前内容
[D]    清空      — 清空当前内容（可撤销）
[Z]    撤销      — 回滚到上一版本
[X]    重做      — 前进到下一版本
[C]    复制      — 手动复制到剪贴板
[Q]    退出

录音结束后可输入专业名词（空格分隔），AI会用其纠正转写错误
"""

import os
import shutil
import sys
import subprocess
import textwrap
import time

from synclisten.core.recorder import AudioRecorder
from synclisten.core.transcriber import SenseVoiceTranscriber
from synclisten.core.ai_client import AIClient

# ── 跨平台即时按键读取 ──────────────────────────────────
def _is_tty():
    return sys.stdin.isatty()


if sys.platform == "win32":
    import msvcrt

    def _getch_raw():
        return msvcrt.getch().decode("utf-8", errors="ignore")
else:
    import tty
    import termios

    def _getch_raw():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch


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


def redraw(session, transient="", hint="", input_buffer=None):
    """清屏并重绘分区：内容区 + 临时区 + 输入区 + 操作栏。

    Args:
        input_buffer: 如果为字符串（包括空串），则显示输入区，
                      None 则不显示输入区。
    """
    _clear()
    w = _term_width()

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

    # 底部操作栏
    print("[↵]✍️  [A]🤖  [D]🗑  [Z]↩️  [X]↪️  [C]📋  [Q]👋")
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

        # ESC 取消（并吃掉可能的转义序列余下字符）
        if ch == "\x1b":
            while True:
                try:
                    nxt = _getch_raw()
                    if nxt in ("\x00", "\xe0") or nxt.isalpha():
                        break
                except (EOFError, KeyboardInterrupt):
                    break
            return ""

        # 退格 / DEL
        if ch in ("\x7f", "\b"):
            buf = buf[:-1]
            redraw(session, transient=transient, input_buffer=buf)
            continue

        # 可打印字符（排除控制字符）
        if ord(ch) >= 32:
            buf += ch
            redraw(session, transient=transient, input_buffer=buf)


def _apply_terminology(text, terminology):
    """无 AI 时，用用户输入的术语对转写文本做简单替换。

    支持两种格式（空格分隔）：
      - 直接给出正确词，程序在文本中找最相似的词替换
      - 错词->对词，直接做字符串替换
    """
    if not terminology or not text:
        return text

    terms = [t.strip() for t in terminology.split() if t.strip()]
    if not terms:
        return text

    result = text

    for term in terms:
        if "->" in term:
            # 显式替换规则
            wrong, correct = term.split("->", 1)
            wrong = wrong.strip()
            correct = correct.strip()
            if wrong:
                result = result.replace(wrong, correct)
        else:
            # 模糊替换：在文本中找长度相近、字符重叠度高的词
            words = list(set(result.split()))  # 去重，避免重复替换
            best_match = None
            best_score = 0
            for word in words:
                if len(word) < 2:
                    continue
                len_diff = abs(len(word) - len(term))
                if len_diff > 2:
                    continue
                # 简单相似度：共同字符比例
                common = len(set(word.lower()) & set(term.lower()))
                score = common / max(len(word), len(term))
                if score > best_score and score >= 0.5:
                    best_score = score
                    best_match = word

            if best_match:
                result = result.replace(best_match, term)

    return result


def cmd_write(session, recorder, transcriber, ai_client):
    """写入模式：录音 → 转写 → 输入专业名词 → AI 润色 → 追加，压入历史。"""
    redraw(session, hint="🔴 录音中… 按回车停止")
    recorder.start()
    _wait_for_enter()

    redraw(session, hint="⏳ 转写中…")
    audio = recorder.stop()
    if audio is None or len(audio) == 0:
        redraw(session, hint="⚠️ 未检测到音频")
        return

    raw_text, _ = transcriber.transcribe(audio)
    if not raw_text:
        redraw(session, hint="⚠️ 未识别到文字")
        return

    # 专业名词输入阶段
    terminology = _read_input_line(session, transient=raw_text)

    # AI 润色（带上专业名词）
    if ai_client is not None:
        redraw(session, transient=raw_text, hint="⏳ 润色中…")
        try:
            text = ai_client.polish_text(raw_text, terminology=terminology or None)
        except Exception:
            text = raw_text
    else:
        # 无 AI 时做简单替换
        text = _apply_terminology(raw_text, terminology) if terminology else raw_text

    new_content = session.content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += text
    session.commit(new_content)
    copy_to_clipboard(session.content)
    redraw(session, hint=f"✅ 已写入 ({len(session.content)}字)")


def cmd_ai(session, recorder, transcriber, ai_client):
    """AI 指令模式：录音为指令 → 输入专业名词 → AI 处理 → 覆盖内容，压入历史。"""
    redraw(session, hint="🔴 说出指令… 按回车停止")
    recorder.start()
    _wait_for_enter()

    redraw(session, hint="⏳ 转写中…")
    audio = recorder.stop()
    if audio is None or len(audio) == 0:
        redraw(session, hint="⚠️ 未检测到音频")
        return

    instruction, _ = transcriber.transcribe(audio)
    if not instruction:
        redraw(session, hint="⚠️ 未识别到文字")
        return

    # 专业名词输入阶段
    terminology = _read_input_line(session, transient=f"📋 {instruction}")

    redraw(session, transient=f"📋 {instruction}", hint="⏳ AI 处理中…")
    try:
        result = ai_client.process_document(
            session.content, instruction, terminology=terminology or None
        )
        session.commit(result)
        copy_to_clipboard(session.content)
        redraw(session, hint="✅ AI 已覆盖")
    except Exception as e:
        redraw(session, hint=f"❌ {e}")


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


def main():
    session = Session()

    print("🔄 加载 SenseVoice…")
    transcriber = SenseVoiceTranscriber()
    recorder = AudioRecorder()

    ai_client = None
    try:
        ai_client = AIClient()
        print("✅ AI 就绪")
    except ValueError as e:
        print(f"⚠️  AI 未配置: {e}")
        print("   A 模式不可用")

    time.sleep(0.5)
    redraw(session)

    while True:
        try:
            ch = _getch()
        except (EOFError, KeyboardInterrupt):
            _clear()
            break

        # 忽略非预期的控制字符（如 Esc 序列开头）
        if ch == "\x1b":
            # 吃掉可能的 Esc 序列剩余字符
            while True:
                try:
                    nxt = _getch()
                    if nxt in ("\x00", "\xe0") or nxt.isalpha():
                        break
                except (EOFError, KeyboardInterrupt):
                    break
            continue

        choice = ch.lower()

        if choice == "\r" or choice == "\n":
            cmd_write(session, recorder, transcriber, ai_client)
        elif choice == "a":
            if ai_client is None:
                redraw(session, hint="⚠️ AI 未配置")
            else:
                cmd_ai(session, recorder, transcriber, ai_client)
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
            redraw(session, hint=f"? 未知按键: {repr(ch)}")


if __name__ == "__main__":
    main()
