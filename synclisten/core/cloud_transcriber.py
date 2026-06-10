# -*- coding: utf-8 -*-
"""阿里云 DashScope Paraformer 在线流式语音转写。

一边录一边传：录音回调里产生的 PCM 帧通过 feed() 实时送入识别，
按回车停止后 finalize() 立即拿到结果（大部分音频在录制期间已转写完），
把停止到出文字的延迟降到最低。

不在模块顶层 import dashscope：与本地 funasr 同理，避免拖慢启动；
仅在真正建立识别会话时再导入。
"""

import threading

from ..config import DASHSCOPE_API_KEY, PARAFORMER_MODEL, SAMPLE_RATE


class CloudTranscriber:
    """Paraformer 实时识别器（轻量，不加载任何本地模型）。"""

    def __init__(self, api_key=None, model=None):
        self.api_key = api_key or DASHSCOPE_API_KEY
        self.model = model or PARAFORMER_MODEL
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY 未设置，无法使用云端转写。")
        self._recognition = None
        self._sentences = []
        self._lock = threading.Lock()
        self._error = None

    @property
    def failed(self):
        """识别过程中是否发生过错误（含中途断网）。"""
        return self._error is not None

    def begin(self):
        """建立识别会话。若网络不可用，start() 会很快抛异常。"""
        import dashscope
        from dashscope.audio.asr import (
            Recognition,
            RecognitionCallback,
            RecognitionResult,
        )

        dashscope.api_key = self.api_key
        with self._lock:
            self._sentences = []
        self._error = None
        outer = self

        class _CB(RecognitionCallback):
            def on_event(self, result):
                try:
                    sentence = result.get_sentence()
                    if not sentence or "text" not in sentence:
                        return
                    # 仅收录“句子结束”的最终结果，避免重复累计中间态
                    if RecognitionResult.is_sentence_end(sentence):
                        with outer._lock:
                            outer._sentences.append(sentence["text"])
                except Exception:
                    pass

            def on_error(self, result):
                outer._error = getattr(result, "message", "recognition error")

        self._recognition = Recognition(
            model=self.model,
            format="pcm",
            sample_rate=SAMPLE_RATE,
            callback=_CB(),
        )
        self._recognition.start()

    def feed(self, pcm_bytes):
        """送入一帧 16-bit PCM。由录音回调线程调用，必须轻量且不抛出。

        发送失败（多半是中途断网）记录到 _error，由调用方据 failed 判定回退。
        """
        r = self._recognition
        if r is None or self._error is not None:
            return
        try:
            r.send_audio_frame(pcm_bytes)
        except Exception as e:
            self._error = str(e)

    def finalize(self):
        """停止会话并返回累计文本（失败时返回已得到的部分）。"""
        if self._recognition is not None:
            try:
                self._recognition.stop()
            except Exception as e:
                if self._error is None:
                    self._error = str(e)
            self._recognition = None
        with self._lock:
            return "".join(self._sentences).strip()
