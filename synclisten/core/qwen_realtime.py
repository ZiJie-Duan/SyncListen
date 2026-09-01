# -*- coding: utf-8 -*-
"""Qwen3-ASR-Flash-Realtime 流式识别：WebSocket 边说边出字，带上下文注入。

协议（DashScope Realtime API，用 SDK 的 OmniRealtimeConversation 封装）：
  connect → session.update（服务端 VAD、pcm 16 kHz、corpus 上下文）
  → 持续 append 音频 → 服务端按停顿切句：
      每句先高频推送 conversation.item.input_audio_transcription.text
        （text = 已确认前缀，stash = 仍可能修正的草稿；实时预览 = text + stash）
      停顿后推送 …transcription.completed（transcript = 该句定稿）
  → session.finish → 服务端补完最后一句 → session.finished → 客户端断开
  （该模型是会话模式，每次录音一条连接，结束即断开，不复用。）

崩溃/断网契约：
  · 建连或会话配置失败在 begin() 抛出，调用方回退分段转写（录音尚未开始，无损）；
  · 中途出错（断网、服务端 error 事件、连接被关）只记入 error，不抛到录音回调；
    covered_seconds() 给出"定稿已覆盖到的音频位置"（来自 VAD 的 audio_end_ms），
    调用方只对其后的音频做分段兜底——已定稿的句子不丢也不重复；
  · 送音频不在录音回调里做网络 I/O：回调只入队，独立发送线程编码+发送，
    网络卡顿不会阻塞 PortAudio 回调造成丢音频；
  · SDK 回调线程绝不打印；状态通过带锁的属性暴露给主线程渲染。
"""

import base64
import logging
import queue
import threading

from ..config import (
    DASHSCOPE_API_KEY,
    DASHSCOPE_REALTIME_URL,
    QWEN_ASR_LANGUAGE,
    QWEN_ASR_REALTIME_MODEL,
    REALTIME_VAD_SILENCE_MS,
    REALTIME_VAD_THRESHOLD,
    SAMPLE_RATE,
)
from .qwen_asr import _is_context_echo

# 建连/会话配置的等待上限（秒）：与 SDK connect() 自身的 5 秒握手上限一致。
CONNECT_TIMEOUT_SECONDS = 5.0

_EVT_TEXT = "conversation.item.input_audio_transcription.text"
_EVT_DONE = "conversation.item.input_audio_transcription.completed"
_EVT_FAILED = "conversation.item.input_audio_transcription.failed"
_EVT_SPEECH_STOP = "input_audio_buffer.speech_stopped"
_EVT_SPEECH_START = "input_audio_buffer.speech_started"


def _quiet_sdk_loggers():
    """SDK 与 websocket 库在出错时会走 logging；没有 handler 时 Python 会用
    lastResort 直接打到 stderr、撕裂界面。挂 NullHandler 让它们安静。"""
    for name in ("dashscope", "websocket"):
        lg = logging.getLogger(name)
        if not lg.handlers:
            lg.addHandler(logging.NullHandler())
        lg.propagate = False


class QwenRealtimeTranscriber:
    """一次录音一条实时识别会话：begin() → feed()… → finalize()。"""

    def __init__(self, api_key=None, model=None, url=None):
        self.api_key = api_key or DASHSCOPE_API_KEY
        self.model = model or QWEN_ASR_REALTIME_MODEL
        self.url = url or DASHSCOPE_REALTIME_URL
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY 未设置，无法使用 Qwen 实时转写。")
        self._lock = threading.Lock()
        self._reset_state()

    def _reset_state(self):
        self._conv = None
        self._context = ""
        self._error = None
        self._closed = False
        self._finishing = False
        self._configured = threading.Event()
        self._finals = []              # 各句定稿
        self._preview = ""             # 当前句预览（text + stash）
        self._speech_active = False
        self._last_stop_ms = 0         # 最近一次 VAD 语音结束位置（毫秒）
        self._covered_ms = 0           # 定稿已覆盖到的音频位置（毫秒）
        self._marks_ms = []            # 每次定稿的位置（毫秒），供 HUD 标断句点
        self._q = queue.Queue()
        self._sender = None
        self.dropped_frames = 0        # 出错后丢弃的音频帧数（仅统计）

    # ── 状态（主线程读）────────────────────────────────
    @property
    def failed(self):
        return self._error is not None

    @property
    def error(self):
        return self._error

    def final_text(self):
        with self._lock:
            return "".join(self._finals)

    def preview_text(self):
        with self._lock:
            return self._preview

    def text(self):
        """定稿 + 当前句预览。"""
        with self._lock:
            return "".join(self._finals) + self._preview

    def segments(self):
        with self._lock:
            return len(self._finals)

    def speech_active(self):
        with self._lock:
            return self._speech_active

    def covered_seconds(self):
        """定稿已覆盖到的音频秒数（其后的音频尚无定稿）。"""
        with self._lock:
            return self._covered_ms / 1000.0

    def mark_seconds(self):
        """各次定稿落点的秒数（断句位置），供时间轴标记。"""
        with self._lock:
            return [ms / 1000.0 for ms in self._marks_ms]

    # ── 生命周期 ──────────────────────────────────────
    def begin(self, context=""):
        """建连并配置会话。失败抛异常（此时录音尚未开始，调用方可无损回退）。"""
        import dashscope
        from dashscope.audio.qwen_omni import OmniRealtimeConversation, MultiModality
        from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams

        _quiet_sdk_loggers()
        self._reset_state()
        self._context = context or ""
        dashscope.api_key = self.api_key
        outer = self

        # SDK 的回调基类只在需要时导入
        from dashscope.audio.qwen_omni import OmniRealtimeCallback

        class _CB(OmniRealtimeCallback):
            def on_open(self):
                pass

            def on_close(self, code, msg):
                outer._on_close(code, msg)

            def on_event(self, message):
                try:
                    outer._on_event(message)
                except Exception:
                    pass  # 回调线程里任何异常都不能外泄

        conv = OmniRealtimeConversation(
            model=self.model, url=self.url, callback=_CB(), api_key=self.api_key
        )
        conv.connect()  # 5 秒内握手不成功抛 TimeoutError
        self._conv = conv
        params = TranscriptionParams(
            language=QWEN_ASR_LANGUAGE or None,
            sample_rate=SAMPLE_RATE,
            input_audio_format="pcm",
            corpus_text=self._context or None,
        )
        conv.update_session(
            output_modalities=[MultiModality.TEXT],
            enable_turn_detection=True,
            turn_detection_type="server_vad",
            turn_detection_threshold=REALTIME_VAD_THRESHOLD,
            turn_detection_silence_duration_ms=REALTIME_VAD_SILENCE_MS,
            enable_input_audio_transcription=True,
            transcription_params=params,
        )
        # 等 session.updated（或 error）：配置被拒时在这里就失败，而不是录到一半
        self._configured.wait(CONNECT_TIMEOUT_SECONDS)
        if self._error is not None:
            err = self._error
            self.close()
            raise RuntimeError(f"实时识别会话配置失败：{err}")
        self._sender = threading.Thread(target=self._send_loop, name="qwen-rt-send", daemon=True)
        self._sender.start()

    def feed(self, pcm_bytes):
        """录音回调调用：只入队，不做网络 I/O，永不抛出。"""
        if self._error is not None or self._closed:
            self.dropped_frames += 1
            return
        self._q.put(pcm_bytes)

    def _send_loop(self):
        conv = self._conv
        while True:
            item = self._q.get()
            if item is None:
                return
            if self._error is not None or self._closed:
                continue
            try:
                conv.append_audio(base64.b64encode(item).decode("ascii"))
            except Exception as e:
                self._error = f"发送音频失败：{e}"

    def finalize(self, timeout=10.0):
        """停止送音频，通知服务端收尾并等最后一句定稿；返回全部定稿文本。

        超时/出错记入 error（调用方据 covered_seconds() 对剩余音频兜底）。
        """
        conv = self._conv
        if conv is None:
            return self.final_text()
        # 先让发送线程把队列发完
        if self._sender is not None and self._sender.is_alive():
            self._q.put(None)
            self._sender.join(timeout=timeout)
            if self._sender.is_alive() and self._error is None:
                self._error = "发送音频超时"
        self._finishing = True
        if self._error is None and not self._closed:
            try:
                conv.end_session(timeout=int(max(1, timeout)))
            except Exception as e:
                self._error = f"会话收尾失败：{e}"
        self.close()
        return self.final_text()

    def close(self):
        """无条件断开（幂等）。"""
        self._finishing = True
        conv, self._conv = self._conv, None
        if self._sender is not None and self._sender.is_alive():
            self._q.put(None)
        if conv is not None:
            try:
                conv.close()
            except Exception:
                pass
        self._closed = True

    # ── SDK 回调（websocket 线程）───────────────────────
    def _on_close(self, code, msg):
        self._closed = True
        if not self._finishing and self._error is None:
            self._error = f"连接已关闭（{code} {msg})" if code or msg else "连接已关闭"

    def _on_event(self, m):
        t = m.get("type")
        if t == _EVT_TEXT:
            with self._lock:
                self._preview = (m.get("text") or "") + (m.get("stash") or "")
        elif t == _EVT_DONE:
            transcript = (m.get("transcript") or "").strip()
            if transcript and self._context and _is_context_echo(transcript, self._context):
                transcript = ""  # 无有效语音时模型回吐 context 的伪结果
            with self._lock:
                if transcript:
                    self._finals.append(transcript)
                self._preview = ""
                self._covered_ms = max(self._covered_ms, self._last_stop_ms)
                if self._covered_ms:
                    self._marks_ms.append(self._covered_ms)
        elif t == _EVT_SPEECH_START:
            with self._lock:
                self._speech_active = True
        elif t == _EVT_SPEECH_STOP:
            end_ms = m.get("audio_end_ms")
            with self._lock:
                self._speech_active = False
                if isinstance(end_ms, (int, float)):
                    self._last_stop_ms = int(end_ms)
        elif t == "session.updated":
            self._configured.set()
        elif t == _EVT_FAILED:
            err = m.get("error") or {}
            self._error = f"识别失败：{err.get('message') or err.get('code') or '未知错误'}"
            self._configured.set()
        elif t == "error":
            err = m.get("error") or {}
            self._error = f"{err.get('code') or 'error'}: {err.get('message') or ''}".strip()
            self._configured.set()
