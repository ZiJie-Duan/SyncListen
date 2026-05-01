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


def redraw(session, transient="", hint=""):
    """清屏并重绘分区：内容区 + 临时区 + 操作栏。"""
    _clear()
    w = _term_width()

    # 上半区：当前内容
    print("─" * w)
    for line in _fmt_content(session.content, w):
        print(line)
    print("─" * w)

    # 下半区：临时转写 / 状态提示
    if transient:
        wrapped = _wrap_text(transient, w - 3)  # 预留 🎙 前缀
        for i, line in enumerate(wrapped):
            prefix = "🎙 " if i == 0 else "   "
            print(f"{prefix}{line}")
        print("─" * w)

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


def cmd_write(session, recorder, transcriber, ai_client):
    """写入模式：录音 → 转写 → AI 润色 → 追加，压入历史。"""
    redraw(session, hint="🔴 录音中… 按回车停止")
    recorder.start()
    try:
        input()
    except EOFError:
        pass

    redraw(session, hint="⏳ 转写中…")
    audio = recorder.stop()
    if audio is None or len(audio) == 0:
        redraw(session, hint="⚠️ 未检测到音频")
        return

    raw_text, _ = transcriber.transcribe(audio)
    if not raw_text:
        redraw(session, hint="⚠️ 未识别到文字")
        return

    if ai_client is not None:
        redraw(session, transient=raw_text, hint="⏳ 润色中…")
        try:
            text = ai_client.polish_text(raw_text)
        except Exception:
            text = raw_text
    else:
        text = raw_text

    new_content = session.content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += text
    session.commit(new_content)
    copy_to_clipboard(session.content)
    redraw(session, hint=f"✅ 已写入 ({len(session.content)}字)")


def cmd_ai(session, recorder, transcriber, ai_client):
    """AI 指令模式：录音为指令 → AI 处理 → 覆盖内容，压入历史。"""
    redraw(session, hint="🔴 说出指令… 按回车停止")
    recorder.start()
    try:
        input()
    except EOFError:
        pass

    redraw(session, hint="⏳ 转写中…")
    audio = recorder.stop()
    if audio is None or len(audio) == 0:
        redraw(session, hint="⚠️ 未检测到音频")
        return

    instruction, _ = transcriber.transcribe(audio)
    if not instruction:
        redraw(session, hint="⚠️ 未识别到文字")
        return

    redraw(session, transient=f"📋 {instruction}", hint="⏳ AI 处理中…")
    try:
        result = ai_client.process_document(session.content, instruction)
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
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            _clear()
            break

        if choice == "":
            cmd_write(session, recorder, transcriber, ai_client)
        elif choice in ("a",):
            if ai_client is None:
                redraw(session, hint="⚠️ AI 未配置")
            else:
                cmd_ai(session, recorder, transcriber, ai_client)
        elif choice in ("z",):
            cmd_undo(session)
        elif choice in ("x",):
            cmd_redo(session)
        elif choice in ("d",):
            cmd_clear(session)
        elif choice in ("c",):
            cmd_copy(session)
        elif choice in ("q",):
            _clear()
            break
        else:
            redraw(session, hint="?")


if __name__ == "__main__":
    main()
