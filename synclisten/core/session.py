# -*- coding: utf-8 -*-
"""会话状态与崩溃安全持久化。

核心原则：崩溃可以，录完的字不能丢。每次 commit/undo/redo 都原子落盘
（临时文件 + fsync + os.replace）；下次启动无条件恢复上次的完整历史栈。
启动时屏幕从空白开始、上次内容留在历史里，[Z] 一步找回（start_fresh）。

落盘失败（磁盘满等）不抛异常、不阻断操作：内容仍在内存并照常进剪贴板，
save_error 置为原因，由界面持续显示警告直到下一次成功落盘。
"""

import json
import os
from datetime import datetime

from ..config import SESSION_FILE, SESSION_HISTORY_LIMIT
from . import errlog


class Session:
    """文稿 + 历史栈（undo/redo），每次变更立即原子写盘。"""

    def __init__(self, path=None):
        self.path = path or SESSION_FILE
        self.save_error = None    # 最近一次落盘失败的原因（None = 正常）
        history, pos, self.saved_at, self.load_warning = self._load()
        self.history = history
        self.pos = pos
        self.content = self.history[self.pos]

    # ── 持久化 ────────────────────────────────────────
    def _load(self):
        """返回 (history, pos, saved_at, warning)。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            history = [str(x) for x in raw["history"]]
            pos = int(raw["pos"])
            if not history or pos < 0 or pos >= len(history):
                raise ValueError("history/pos 结构无效")
            return history, pos, str(raw.get("saved_at", "")), None
        except FileNotFoundError:
            return [""], 0, "", None
        except (json.JSONDecodeError, OSError, ValueError, KeyError, TypeError) as e:
            # 文件损坏：挪到一边留证（优先可恢复，不删任何东西），从空白开始
            warning = f"会话文件损坏（{e}），已另存为 .corrupt-* 并从空白开始"
            try:
                stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                os.replace(self.path, f"{self.path}.corrupt-{stamp}")
            except OSError:
                pass
            return [""], 0, "", warning

    def save(self):
        """原子落盘。返回是否成功；失败记入 save_error 与日志，不抛。"""
        tmp = self.path + ".tmp"
        payload = {
            "version": 1,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "pos": self.pos,
            "history": self.history,
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            self.save_error = str(e)
            errlog.log_exception("会话落盘失败")
            return False
        self.saved_at = payload["saved_at"]
        self.save_error = None
        return True

    # ── 会话状态 ──────────────────────────────────────
    def commit(self, new_content):
        """提交新版本，丢弃当前位置之后的旧版本，并立即落盘。"""
        self.history = self.history[:self.pos + 1]
        self.history.append(new_content)
        if len(self.history) > SESSION_HISTORY_LIMIT:
            self.history = self.history[-SESSION_HISTORY_LIMIT:]
        self.pos = len(self.history) - 1
        self.content = new_content
        self.save()

    def undo(self):
        if self.pos > 0:
            self.pos -= 1
            self.content = self.history[self.pos]
            self.save()
            return True
        return False

    def redo(self):
        if self.pos < len(self.history) - 1:
            self.pos += 1
            self.content = self.history[self.pos]
            self.save()
            return True
        return False

    def start_fresh(self):
        """启动时调用：屏幕从空白开始，上次内容留在历史里（[Z] 一步找回）。

        返回按 [Z] 可找回的上一版本字数（0 = 没有可找回的内容）。
        """
        if self.content:
            self.commit("")
        return len(self.history[self.pos - 1]) if self.pos > 0 else 0
