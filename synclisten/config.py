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
#   qwen-realtime —— Qwen3-ASR-Flash-Realtime（默认）。WebSocket 真流式：边说边出字
#                    （临时结果约 0.3s 上屏，停顿后定稿），服务端 VAD 切句，可注入 context
#                    （记忆/已有文稿）。建连失败或中途断开自动回退到分段转写。
#   qwen          —— Qwen3-ASR-Flash 一次性识别，按 CHUNK_TARGET_SECONDS 分段送识别
#                    （边录边转、按段上屏），同样带 context。实时引擎的兜底路径。
#   paraformer    —— Paraformer 实时识别。真流式，但不支持 context；保底/对比用。
ONLINE_ASR_ENGINE = os.environ.get("ONLINE_ASR_ENGINE", "qwen-realtime").strip().lower()

# Qwen3-ASR-Flash：走 DashScope 的 OpenAI 兼容接口（复用 openai SDK），
# 鉴权用 DASHSCOPE_API_KEY（与 AI 对话用的 OPENAI_API_KEY 分开）。
QWEN_ASR_MODEL = os.environ.get("QWEN_ASR_MODEL", "qwen3-asr-flash")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
# Qwen3-ASR-Flash-Realtime：DashScope Realtime WebSocket（北京地域；新加坡为
# wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime）。
QWEN_ASR_REALTIME_MODEL = os.environ.get("QWEN_ASR_REALTIME_MODEL", "qwen3-asr-flash-realtime")
DASHSCOPE_REALTIME_URL = os.environ.get(
    "DASHSCOPE_REALTIME_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
)
# 识别语种：空 = 自动检测（中英混读用自动）；可设 zh / en / yue / ja …
QWEN_ASR_LANGUAGE = os.environ.get("QWEN_ASR_LANGUAGE", "").strip()
# 服务端 VAD 参数——取官方文档的推荐值（threshold 推荐 0.0；断句静音推荐 400 ms，
# 取值范围 [200, 6000]）。不是本地拍脑袋的阈值，是服务端模型的调参接口。
REALTIME_VAD_THRESHOLD = float(os.environ.get("REALTIME_VAD_THRESHOLD", "0.0"))
REALTIME_VAD_SILENCE_MS = int(os.environ.get("REALTIME_VAD_SILENCE_MS", "400"))
# ASR context（背景文本）拼装时，已写入文稿最多截取的字符数（取最近窗口）。
ASR_CONTEXT_DOC_LIMIT = int(os.environ.get("ASR_CONTEXT_DOC_LIMIT", "2000"))

PARAFORMER_MODEL = os.environ.get("PARAFORMER_MODEL", "paraformer-realtime-v2")

# ── 分段转写（qwen 引擎 / 离线本地 / 实时引擎的兜底路径）────────────────
# 录音期间每累计约这么多秒、且切点落在语音停顿处，就切出一段送识别（边录边转、
# 按段上屏）；全部段转写完成后才进入后续 AI 流程。长录音不再受云端单次 3 分钟限制。
CHUNK_TARGET_SECONDS = float(os.environ.get("CHUNK_TARGET_SECONDS", "5"))

# ── 录音时间轴（HUD 里的"磁带"）────────────────────────────────────────
# 时间片长度：时间轴必须能分辨的最短事件是"够触发断句的停顿"（REALTIME_VAD_SILENCE_MS），
# 要在这段停顿里落到至少两格才看得出谷底，所以取它的一半。不是拍脑袋的数字。
LEVEL_SLICE_SECONDS = REALTIME_VAD_SILENCE_MS / 1000.0 / 2.0

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

# ── 崩溃安全持久化（会话快照 / 原始录音）─────────────────
# 状态目录遵循 XDG_STATE_HOME（与编辑草稿同根）。核心原则：崩溃可以，
# 录完的东西不能丢——每次提交/撤销立即原子落盘，录音全程同步写流水账。
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "synclisten",
)
SESSION_FILE = os.environ.get("SESSION_FILE", os.path.join(STATE_DIR, "session.json"))
AUDIO_DIR = os.environ.get("AUDIO_DIR", os.path.join(STATE_DIR, "audio"))
# 录音 WAV 留存数量上限，超出按 mtime 从旧到新清理（0 = 全部保留）。
# 转写失败/中断/异常的录音会被标记为 *.keep.wav，不参与清理（那才是最需要保住的）。
AUDIO_KEEP_COUNT = int(os.environ.get("AUDIO_KEEP_COUNT", "50"))
# 录音流水账 fsync 间隔（秒）。每块音频直写内核（无用户态缓冲），进程崩溃零丢失；
# fsync 决定断电/内核崩溃时最多丢多长——这是有界丢失窗口的策略参数。
JOURNAL_SYNC_SECONDS = float(os.environ.get("JOURNAL_SYNC_SECONDS", "1"))
# 会话历史（撤销）深度上限。每个版本是全文快照，无上限会让会话文件无界增长、
# 每次操作全量重写越来越慢；超出时丢弃最老的版本。
SESSION_HISTORY_LIMIT = int(os.environ.get("SESSION_HISTORY_LIMIT", "200"))

# ── 终端视觉效果 ──────────────────────────────────────
# SYNCLISTEN_FX=0（或 NO_COLOR 非空）关闭颜色与动画，退化为纯文本界面。
# 主界面空闲这么多秒后进入矩阵雨屏保（任意键唤醒，该键不作为命令）；0 = 关闭。
FX_IDLE_SECONDS = float(os.environ.get("FX_IDLE_SECONDS", "120"))

# ── 应用配置 ──────────────────────────────────────────
APP_NAME = "SyncListen"
LOG_DIR = os.path.expanduser("~/.config/synclisten/logs")

# 确保目录存在
os.makedirs(LOG_DIR, exist_ok=True)
