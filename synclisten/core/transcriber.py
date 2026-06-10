# -*- coding: utf-8 -*-
"""SenseVoice 语音转文字模块。"""

import numpy as np

from ..config import SENSEVOICE_MODEL, SENSEVOICE_DEVICE, SAMPLE_RATE

# 注意：不在模块顶层 import funasr。
# funasr 会连带加载 torch/sklearn 等，约需 5 秒，若放顶层会让程序
# 在打印任何提示前黑屏干等。改为在模型加载时（已有提示）再 import。


class SenseVoiceTranscriber:
    """基于 SenseVoice 的语音识别器（单例）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        print("🔄 正在加载 SenseVoice 模型…")
        from funasr import AutoModel  # 延迟导入：约 5 秒，放在提示之后

        self.model = AutoModel(
            model=SENSEVOICE_MODEL,
            trust_remote_code=True,
            device=SENSEVOICE_DEVICE,
            disable_update=True,  # 跳过 funasr 联网检查更新，省一次网络往返
        )
        self._initialized = True
        print("✅ 模型加载完成\n")

    def transcribe(self, audio, language="auto"):
        """将音频数据转写为文本。

        Args:
            audio: numpy array, float32, 16kHz 单声道
            language: "auto" | "zh" | "en" | "yue" | "ja" | "ko"

        Returns:
            (text, info) 元组，info 包含检测到的语言等信息
        """
        res = self.model.generate(
            input=audio,
            input_fs=SAMPLE_RATE,
            language=language,
            use_itn=True,
        )

        # res 是列表，取第一个结果
        result = res[0] if isinstance(res, list) else res
        text = result.get("text", "").strip() if isinstance(result, dict) else str(result).strip()

        # 提取检测到的语言信息（用于兼容旧接口）
        info = type("Info", (), {"language": result.get("language", language)})()

        return text, info
