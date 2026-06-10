# -*- coding: utf-8 -*-
"""音频录制模块"""

import sys
import threading
import numpy as np
import sounddevice as sd

from ..config import SAMPLE_RATE


class AudioRecorder:
    """基于 sounddevice 的音频录制器，支持后台获取缓冲区。"""

    def __init__(self, samplerate=SAMPLE_RATE, channels=1):
        self.samplerate = samplerate
        self.channels = channels
        self.frames = []
        self.stream = None
        self._on_frame = None
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"  ⚠ {status}", file=sys.stderr)
        # 始终缓存完整音频：即便在线流式失败，也能用它做离线兜底
        with self._lock:
            self.frames.append(indata.copy())
        # 流式回调（在线 Paraformer）：把该帧转成 16-bit PCM 实时送出
        cb = self._on_frame
        if cb is not None:
            try:
                mono = indata[:, 0] if indata.ndim > 1 else indata
                pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                cb(pcm)
            except Exception:
                # 流式发送出问题不能影响录音；buffer 仍可兜底
                pass

    def start(self, on_frame=None):
        """开始录音。

        Args:
            on_frame: 可选回调，每个音频块就绪时以 16-bit PCM bytes 调用一次，
                      用于一边录一边传（在线流式转写）。为 None 时退化为纯缓存。
        """
        self._on_frame = on_frame
        with self._lock:
            self.frames = []
        self.stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="float32",
            callback=self._callback,
        )
        self.stream.start()

    def get_buffer(self):
        """获取当前已录制的音频（不停止录音）。"""
        with self._lock:
            if not self.frames:
                return np.array([], dtype=np.float32)
            return np.concatenate(self.frames, axis=0).flatten()

    def stop(self):
        """停止录音并返回音频数据。"""
        self._on_frame = None
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        with self._lock:
            if not self.frames:
                return None
            audio = np.concatenate(self.frames, axis=0).flatten()
            self.frames = []
            return audio
