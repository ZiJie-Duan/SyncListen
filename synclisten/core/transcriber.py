# -*- coding: utf-8 -*-
"""SenseVoice 语音转文字模块。"""

import threading

from ..config import SENSEVOICE_MODEL, SENSEVOICE_DEVICE, SAMPLE_RATE

# 注意：不在模块顶层 import funasr。
# funasr 会连带加载 torch/sklearn 等，约需 5 秒，若放顶层会让程序
# 在打印任何提示前黑屏干等。改为首次实例化时（调用方已显示提示）再 import。


class SenseVoiceTranscriber:
    """基于 SenseVoice 的语音识别器（进程内单例，线程安全）。

    可能从分段转写的工作线程与主线程同时首次触发加载（如中断后立刻再录），
    用锁保证模型只加载一次；加载失败不留半成品实例，下次再试。
    不直接打印加载提示（工作线程里打印会与主线程重绘抢屏），进度由调用方显示。
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._load()
                cls._instance = inst
            return cls._instance

    def _load(self):
        from funasr import AutoModel  # 延迟导入：约 5 秒

        self.model = AutoModel(
            model=SENSEVOICE_MODEL,
            trust_remote_code=True,
            device=SENSEVOICE_DEVICE,
            disable_update=True,  # 跳过 funasr 联网检查更新，省一次网络往返
        )

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
