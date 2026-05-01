# -*- coding: utf-8 -*-
"""全局配置"""

import os

# 加载 .env 文件（如果存在）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── SenseVoice 配置 ───────────────────────────────────
SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
SENSEVOICE_DEVICE = "cpu"
SAMPLE_RATE = 16000

# ── AI API 配置 ───────────────────────────────────────
# 支持 OpenAI / DeepSeek / 其他兼容 OpenAI API 格式的服务
# 优先级: 环境变量 > .env 文件 > 默认值
AI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
AI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
AI_MODEL = os.environ.get("AI_MODEL", "deepseek-chat")

# ── 应用配置 ──────────────────────────────────────────
APP_NAME = "SyncListen"
DOCUMENTS_DIR = os.path.expanduser("~/.config/synclisten/documents")
LOG_DIR = os.path.expanduser("~/.config/synclisten/logs")

# 确保目录存在
os.makedirs(DOCUMENTS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
