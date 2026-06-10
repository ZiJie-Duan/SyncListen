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

# ── 阿里云 DashScope（在线语音转写）──────────────────────
# 在线模式下用云端 STT，精度高、支持中英混读与专名。
# 申请方式见 README；密钥通过环境变量或 .env 配置。
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")

# 在线 ASR 引擎：
#   qwen       —— Qwen3-ASR-Flash（默认）。LLM 架构，中英混读强，可注入 context
#                 （背景文本/专名/记忆）让模型在识别阶段就向术语靠拢；一次性识别，
#                 按回车停止后约 1~3s 出结果（非流式）。
#   paraformer —— Paraformer 实时识别。真流式、边录边传、零延迟感；作为保底/对比。
ONLINE_ASR_ENGINE = os.environ.get("ONLINE_ASR_ENGINE", "qwen").strip().lower()

# Qwen3-ASR-Flash：走 DashScope 的 OpenAI 兼容接口（复用 openai SDK），
# 鉴权用 DASHSCOPE_API_KEY（与 AI 对话用的 OPENAI_API_KEY 分开）。
QWEN_ASR_MODEL = os.environ.get("QWEN_ASR_MODEL", "qwen3-asr-flash")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
# ASR context（背景文本）拼装时，已写入文稿最多截取的字符数（取最近窗口）。
ASR_CONTEXT_DOC_LIMIT = int(os.environ.get("ASR_CONTEXT_DOC_LIMIT", "2000"))

PARAFORMER_MODEL = os.environ.get("PARAFORMER_MODEL", "paraformer-realtime-v2")

# ── 网络探测 ──────────────────────────────────────────
# 探测真实 API 域名的 TCP 可达性（而非泛化 ping），因为我们关心的是
# “那个 API 通不通”。任一目标可达即视为在线。
NETWORK_PROBE_HOSTS = [
    ("dashscope.aliyuncs.com", 443),
]
NETWORK_PROBE_TIMEOUT = 2.0

# ── 长期记忆（连贯记忆 / 衰减记忆）────────────────────────
# 一份持久化、按天分片的滚动记忆，描述用户最近在做/聊什么，作为 ASR 上下文三件套
# 之一注入，提升专名与语境识别。短期专注 + 逐渐遗忘：生成只看最近几天，存储滚动保留。
MEMORY_FILE = os.environ.get(
    "MEMORY_FILE", os.path.expanduser("~/.config/synclisten/memory.json")
)
MEMORY_RETENTION_DAYS = int(os.environ.get("MEMORY_RETENTION_DAYS", "30"))  # 存储保留窗口
MEMORY_REFERENCE_DAYS = int(os.environ.get("MEMORY_REFERENCE_DAYS", "3"))   # 生成参考窗口
MEMORY_DAY_CHAR_LIMIT = int(os.environ.get("MEMORY_DAY_CHAR_LIMIT", "500")) # 每天记忆字数上限
MEMORY_AI_TRIGGER_COUNT = int(os.environ.get("MEMORY_AI_TRIGGER_COUNT", "5"))  # 触发更新的 AI 次数

# ── 应用配置 ──────────────────────────────────────────
APP_NAME = "SyncListen"
DOCUMENTS_DIR = os.path.expanduser("~/.config/synclisten/documents")
LOG_DIR = os.path.expanduser("~/.config/synclisten/logs")

# 确保目录存在
os.makedirs(DOCUMENTS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
