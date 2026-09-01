# -*- coding: utf-8 -*-
"""错误日志：把未捕获异常与关键失败写入日志文件，终端只回显路径。

原则：程序可以报错，但不能因报错而退出，也不能无声吞掉。
所有函数永不抛出——日志本身写不进去（磁盘满等）就静默放弃。
"""

import sys
import threading
import traceback
from datetime import datetime

from ..config import LOG_DIR


def log_exception(context=""):
    """在 except 块中调用：把当前异常追加到今天的日志文件，返回日志路径。

    依赖 traceback.format_exc()，因此必须在 except 块内调用。
    """
    try:
        path = _today_log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {context}\n")
            f.write(traceback.format_exc())
        return path
    except Exception:
        return None


def log_message(context, message=""):
    """记录一条非异常事件（如实时连接中断的原因），返回日志路径。"""
    try:
        path = _today_log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {context}\n")
            if message:
                f.write(str(message).rstrip() + "\n")
        return path
    except Exception:
        return None


def _today_log_path():
    return f"{LOG_DIR}/synclisten-{datetime.now():%Y%m%d}.log"


def setup_hooks():
    """兜住主线程与后台线程的漏网异常：记日志，交还原有处理，不额外崩。"""
    def _sys_hook(tp, val, tb):
        if not issubclass(tp, KeyboardInterrupt):
            log_exception("uncaught exception")
        sys.__excepthook__(tp, val, tb)

    def _thread_hook(args):
        log_exception(f"thread {args.thread.name}: uncaught exception")

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
