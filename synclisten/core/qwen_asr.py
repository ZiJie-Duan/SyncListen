# -*- coding: utf-8 -*-
"""阿里云 DashScope Qwen3-ASR-Flash 在线语音识别（一次性，带上下文注入）。

与 Paraformer（流式）不同：Qwen3-ASR-Flash 是一次性文件识别——录完整段音频后
整体上传，按回车停止后约 1~3s 出结果。优势是 LLM 架构、中英混读强，且可注入
context（背景文本/专有名词/记忆），让模型在“听”的阶段就向这些术语靠拢，对无关
文本鲁棒。

走 OpenAI 兼容接口（DashScope compatible-mode），复用项目已装的 openai SDK；
鉴权用 DASHSCOPE_API_KEY（与 AI 对话用的 OPENAI_API_KEY 分开）。

不在模块顶层 import openai 之外的重依赖；OpenAI 客户端按需创建。
"""

import base64
import io
import re
import wave

import numpy as np

from ..config import (
    DASHSCOPE_API_KEY,
    DASHSCOPE_BASE_URL,
    QWEN_ASR_MODEL,
    SAMPLE_RATE,
)

# context 各部分的标签前缀。既用于拼装注入文本（见 main._build_asr_context），
# 也作为“回吐”判别的指纹：这些是我们注入的管理性字样，正常语音几乎不可能逐字
# 念出，可据此把“模型回吐 context”与“用户真的提到某个术语”区分开。
CONTEXT_MEMORY_LABEL = "近期记忆："
CONTEXT_DOC_LABEL = "已有上下文："
CONTEXT_RUNNING_LABEL = "本段前文："
_CONTEXT_LABELS = (CONTEXT_MEMORY_LABEL, CONTEXT_DOC_LABEL, CONTEXT_RUNNING_LABEL)


def _norm(s):
    """去除空白与标点，仅保留字母/数字/CJK，用于回吐比对。"""
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)


def _is_context_echo(text, context):
    """判断识别结果是否只是把注入的 context 原样回吐。

    实测：Qwen3-ASR-Flash 在“听不到有效语音”（纯静音 / 仅噪声）时，会把注入的
    context 原样吐回当作识别结果——若不拦，一次误录音就会把整段背景文稿污染进文档。

    判据（无任何阈值魔法数）——返回文本同时满足：
      ① 是 context（去标点空白后）的连续子串；且
      ② 含有我们注入的标签字样（热词/专有名词、已有上下文）。
    正常新语句不是 context 的子串；用户单说某个术语词（如 SenseVoice）虽是子串，
    但不含我们的标签，因此不会被误杀。
    """
    nt, nc = _norm(text), _norm(context)
    if not nt or not nc or nt not in nc:
        return False
    return any(_norm(lbl) in nt for lbl in _CONTEXT_LABELS)


def _audio_to_wav_base64(audio, sample_rate=SAMPLE_RATE):
    """float32 单声道 numpy → 16-bit PCM WAV → base64 data-uri。

    Qwen-ASR 限制：16kHz 采样、≤3 分钟、≤10MB。WAV 16kHz 单声道约 1.9MB/分钟，
    base64 膨胀 ~1.33×，3 分钟约 7.6MB，仍在限额内；本工具是“录一句即停”的离散
    话语，体量远小于此。
    """
    mono = audio[:, 0] if getattr(audio, "ndim", 1) > 1 else audio
    pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:audio/wav;base64,{b64}"


class QwenASRTranscriber:
    """Qwen3-ASR-Flash 一次性识别器（轻量，无本地模型）。"""

    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or DASHSCOPE_API_KEY
        self.base_url = base_url or DASHSCOPE_BASE_URL
        self.model = model or QWEN_ASR_MODEL
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY 未设置，无法使用 Qwen 云端转写。")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key, base_url=self.base_url, timeout=60.0
            )
        return self._client

    def transcribe(self, audio, context=""):
        """整段音频识别。

        Args:
            audio: float32、16kHz、单声道 numpy 音频。
            context: 背景文本（专名/已写入文稿/今天的记忆等），可空。通过 system
                     消息注入，模型据此做 context-guided 识别，对无关内容鲁棒。

        Returns:
            识别文本（已 strip）。失败时抛异常，由调用方探测断网并本地兜底。
        """
        if audio is None or len(audio) == 0:
            return ""
        data_uri = _audio_to_wav_base64(audio)

        # 上下文通过 system 消息的背景文本注入（Qwen3-ASR 的 context-guided 识别）；
        # content 用 [{"text": ...}] 形态（DashScope OpenAI 兼容接口的 ASR 写法）。
        messages = [
            {"role": "system", "content": [{"text": context or ""}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": data_uri}},
                ],
            },
        ]
        client = self._get_client()
        completion = client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
            # enable_itn：数字/时间等逆文本规整；enable_lid：自动语种识别（中英混读）
            extra_body={"asr_options": {"enable_itn": True, "enable_lid": True}},
        )
        text = (completion.choices[0].message.content or "").strip()
        # 丢弃“context 回吐”的伪结果（无有效语音时模型会把注入的 context 吐回来）。
        if text and context and _is_context_echo(text, context):
            return ""
        return text
