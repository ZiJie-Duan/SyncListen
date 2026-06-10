# -*- coding: utf-8 -*-
"""长期记忆（连贯记忆 / 衰减记忆）持久化与后台更新。

一份全局、跨重启持久化、按天分片的滚动记忆，描述用户最近在做什么 / 聊什么领域，
作为在线 ASR 的上下文三件套之一注入，提升专名与语境识别。

结构（灵活、不分字段）：
    {
      "version": 1,
      "days": [
        {"date": "2026-06-10", "items": ["用户在写语音转写 CLI SyncListen", ...]}
      ]
    }
每天一条 = 日期 + 自由短句列表；每天 items 总字数 ≤ MEMORY_DAY_CHAR_LIMIT。

衰减机制：
  · 喂给 ASR 的永远只是“今天这一条”（它已吸收前几天的衰减结果）。
  · 今天这条 = 由「最近 MEMORY_REFERENCE_DAYS 天记忆 + 今天已有 + 当前文稿 + 易错词」
    经 LLM 近因加权、选择性遗忘、压缩重写得到；只参考最近几天，更早的不再看。
  · 存储滚动保留 MEMORY_RETENTION_DAYS 天，超出的删掉。
  · 跨天按本机时间自动新建当天条目。

更新在后台线程执行（LLM 调用是网络 I/O），用户无感；完成后由调用方回调闪烁提示。
"""

import json
import os
import threading
from datetime import date, timedelta

from ..config import (
    MEMORY_AI_TRIGGER_COUNT,
    MEMORY_DAY_CHAR_LIMIT,
    MEMORY_FILE,
    MEMORY_REFERENCE_DAYS,
    MEMORY_RETENTION_DAYS,
)


def _today_str():
    return date.today().isoformat()


class MemoryStore:
    """长期记忆的读写、衰减裁剪、AI 计数与后台更新。"""

    def __init__(self, path=None):
        self.path = path or MEMORY_FILE
        self._data = self._load()
        self._ai_counter = 0
        self._data_lock = threading.Lock()    # 保护 _data 读写
        self._update_lock = threading.Lock()  # 串行化后台更新
        self._threads = []                     # 跟踪后台更新线程，退出时等待

    # ── 持久化 ────────────────────────────────────────
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("days"), list):
                days = []
                for d in data["days"]:
                    if isinstance(d, dict) and d.get("date") and isinstance(d.get("items"), list):
                        days.append({"date": str(d["date"]),
                                     "items": [str(x) for x in d["items"] if str(x).strip()]})
                return {"version": 1, "days": days}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"version": 1, "days": []}

    def _save(self):
        """原子写入，避免中途崩溃损坏文件。"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ── 查询 ──────────────────────────────────────────
    def _find_day(self, day):
        for d in self._data["days"]:
            if d["date"] == day:
                return d
        return None

    def today_items(self):
        """今天这条的 items（副本）；不存在返回空列表。"""
        with self._data_lock:
            d = self._find_day(_today_str())
            return list(d["items"]) if d else []

    def today_text(self):
        """今天记忆拍平成一段文本，供 ASR context 注入；无则空串。"""
        items = self.today_items()
        return "；".join(items)

    def recent_days(self, exclude_today=True, limit=None):
        """最近若干天的记录（按日期升序的副本），用于生成参考或 [M] 查看。"""
        limit = MEMORY_REFERENCE_DAYS if limit is None else limit
        today = _today_str()
        with self._data_lock:
            days = sorted(self._data["days"], key=lambda d: d["date"])
            if exclude_today:
                days = [d for d in days if d["date"] < today]
            days = days[-limit:] if limit else days
            return [{"date": d["date"], "items": list(d["items"])} for d in days]

    def all_days(self):
        with self._data_lock:
            return [{"date": d["date"], "items": list(d["items"])}
                    for d in sorted(self._data["days"], key=lambda d: d["date"])]

    # ── 写入 / 衰减 ────────────────────────────────────
    def _enforce_limit(self, items):
        """裁剪今天 items，使总字数 ≤ MEMORY_DAY_CHAR_LIMIT（从尾部丢弃整条；
        单条超限则截断该条）。"""
        out, total = [], 0
        for it in items:
            it = it.strip()
            if not it:
                continue
            if total + len(it) <= MEMORY_DAY_CHAR_LIMIT:
                out.append(it)
                total += len(it)
            else:
                remain = MEMORY_DAY_CHAR_LIMIT - total
                if remain >= 8:  # 还能放下有意义的一截
                    out.append(it[:remain])
                break
        return out

    def set_today(self, items):
        """覆盖今天这条的 items（含字数裁剪、衰减裁剪、落盘）。"""
        items = self._enforce_limit(items)
        with self._data_lock:
            d = self._find_day(_today_str())
            if d is None:
                self._data["days"].append({"date": _today_str(), "items": items})
            else:
                d["items"] = items
            self._prune_locked()
            self._save()

    def clear(self):
        """清空全部记忆。清空前先写一份 .bak 存档（优先可恢复，别丢数据）。"""
        with self._data_lock:
            try:
                if os.path.exists(self.path):
                    os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
            self._data = {"version": 1, "days": []}
            self._save()

    def _prune_locked(self):
        """滚动保留 MEMORY_RETENTION_DAYS 天，更早的删除。调用方须持有 _data_lock。"""
        cutoff = (date.today() - timedelta(days=MEMORY_RETENTION_DAYS - 1)).isoformat()
        self._data["days"] = [d for d in self._data["days"] if d["date"] >= cutoff]

    # ── AI 触发计数 ───────────────────────────────────
    def bump_counter(self):
        """一次 AI 功能后 +1；累计达 MEMORY_AI_TRIGGER_COUNT 则归零并返回 True。"""
        self._ai_counter += 1
        if self._ai_counter >= MEMORY_AI_TRIGGER_COUNT:
            self._ai_counter = 0
            return True
        return False

    def reset_counter(self):
        self._ai_counter = 0

    # ── 后台更新 ──────────────────────────────────────
    def update_async(self, current_doc, terms, ai_client, on_done=None, force=False):
        """在后台线程更新今天的记忆。

        force=True（[D] 删除等关键时刻）：排队等待当前更新结束后必定执行；
        force=False（5 次 AI 触发）：若已有更新在跑则跳过，避免堆积。
        """
        t = threading.Thread(
            target=self._run_update,
            args=(current_doc, terms, ai_client, on_done, force),
            daemon=True,
        )
        self._threads = [x for x in self._threads if x.is_alive()]
        self._threads.append(t)
        t.start()
        return t

    def _run_update(self, current_doc, terms, ai_client, on_done, force):
        if force:
            self._update_lock.acquire()
        elif not self._update_lock.acquire(blocking=False):
            return  # 已有更新在跑，跳过这次 5-count 触发
        try:
            prev = self.recent_days(exclude_today=True, limit=MEMORY_REFERENCE_DAYS)
            today = self.today_items()
            # 无可参考记忆、也无文稿可吸收 → 没东西可更新
            if not prev and not today and not (current_doc or "").strip():
                return
            new_items = ai_client.update_memory(
                recent_days=prev,
                today_items=today,
                current_doc=current_doc or "",
                terms=list(terms or []),
                char_limit=MEMORY_DAY_CHAR_LIMIT,
            )
            if not new_items:
                return
            self.set_today(new_items)
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
        except Exception:
            # 后台更新失败不影响主流程；下次触发再试
            pass
        finally:
            self._update_lock.release()

    def wait_pending(self, timeout=10.0):
        """退出前等待未完成的后台更新落盘（优先可恢复，别丢记忆）。

        返回 True 表示仍有线程在跑（超时未完成）。
        """
        threads = [t for t in self._threads if t.is_alive()]
        if not threads:
            return False
        if timeout <= 0:  # 纯存活检查，不阻塞
            return True
        deadline_each = timeout / len(threads)
        for t in threads:
            t.join(timeout=deadline_each)
        return any(t.is_alive() for t in threads)
