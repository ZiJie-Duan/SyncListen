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
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"  ⚠ {status}", file=sys.stderr)
        with self._lock:
            self.frames.append(indata.copy())

    def start(self):
        """开始录音。"""
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
