# -*- coding: utf-8 -*-
"""全局配置"""

import os

# 加载 .env 文件（如果存在）
# 始终从项目根目录加载，而不是当前工作目录
try:
    from dotenv import load_dotenv
    _project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_project_dir, ".env"))
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

# ── 阿里云 DashScope（Paraformer 在线语音转写）─────────
# 在线模式下用云端实时 STT，边录边传、低延迟、精度高。
# 申请方式见 README；密钥通过环境变量或 .env 配置。
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
PARAFORMER_MODEL = os.environ.get("PARAFORMER_MODEL", "paraformer-realtime-v2")

# ── 网络探测 ──────────────────────────────────────────
# 探测真实 API 域名的 TCP 可达性（而非泛化 ping），因为我们关心的是
# “那个 API 通不通”。任一目标可达即视为在线。
NETWORK_PROBE_HOSTS = [
    ("dashscope.aliyuncs.com", 443),
]
NETWORK_PROBE_TIMEOUT = 2.0

# ── 应用配置 ──────────────────────────────────────────
APP_NAME = "SyncListen"
DOCUMENTS_DIR = os.path.expanduser("~/.config/synclisten/documents")
LOG_DIR = os.path.expanduser("~/.config/synclisten/logs")

# 确保目录存在
os.makedirs(DOCUMENTS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
