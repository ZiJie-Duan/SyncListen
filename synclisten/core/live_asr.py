# -*- coding: utf-8 -*-
"""分段转写调度：录音期间按段送识别，全程按序合并。

一个后台工作线程串行消费段队列：保证顺序、天然限流（不会并发打爆云端）。
每段独立决定引擎：
  · 在线且配置了云端 → 云端；云端出错则本段走本地兜底（无论是否断网——
    断网时顺带切换离线模式，之后的段直接走本地）；
  · 离线 / 未配置云端 → 本地；
  · 本地也失败 → 记该段失败并写日志（原始录音 WAV 已在磁盘，内容不丢）。
云端成功但返回空串（静音段 / 回吐过滤）视为该段无内容，不再跑本地。

工作线程绝不向终端打印（会与主线程的 redraw 抢屏），进度通过 status
属性与结果字典暴露给主线程渲染。
"""

import queue
import threading

from ..config import ASR_CONTEXT_DOC_LIMIT, SAMPLE_RATE
from . import errlog


class LiveTranscriber:
    """按段送转写：submit() 投递，text() 取已确认的合并文本，finish() 收尾。"""

    def __init__(self, engine, base_context="", seed_text=""):
        self.engine = engine
        self.base_context = base_context
        # 本次录音里、在本调度器接手之前已定稿的文本（实时识别热切换而来）：
        # 只作为"本段前文"注入 context，不计入 text()。
        self.seed_text = seed_text or ""
        self.status = ""            # 工作线程写的状态文案（供主线程渲染）
        self.fell_offline = False   # 转写途中确认断网、切了离线模式
        self.cloud_errors = 0       # 网络正常但云端出错、改走本地的段数
        self._results = {}          # seq -> text（可为空串：静音段也算完成）
        self._durations = {}        # seq -> 该段音频时长（秒），供 HUD 算已覆盖进度
        self._failed = []           # 转写彻底失败的段号
        self._total = 0
        self._abandoned = False
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="live-asr", daemon=True)
        self._thread.start()

    # ── 主线程接口 ────────────────────────────────────
    def submit(self, audio):
        """投递一段音频，返回段号。"""
        with self._lock:
            seq = self._total
            self._total += 1
            try:
                self._durations[seq] = audio.shape[0] / float(SAMPLE_RATE)
            except Exception:
                self._durations[seq] = 0.0
        self._queue.put((seq, audio))
        return seq

    def text(self):
        """已确认段的合并文本（按段序）。"""
        with self._lock:
            return "".join(
                self._results[k] for k in sorted(self._results) if self._results[k]
            )

    def done_total(self):
        """(已完成段数含失败, 总段数)。"""
        with self._lock:
            return len(self._results) + len(self._failed), self._total

    def covered_seconds(self):
        """已转写完成的连续前缀所覆盖的音频秒数（中间还有段没回来就不往后算）。"""
        with self._lock:
            total, seq = 0.0, 0
            while seq in self._results or seq in self._failed:
                total += self._durations.get(seq, 0.0)
                seq += 1
            return total

    def failed_count(self):
        with self._lock:
            return len(self._failed)

    def finish(self, abandon_pending=False):
        """收尾：等队列清空并结束工作线程。

        abandon_pending=True（用户中断）时丢弃未处理的段，只给在处理中的
        那段短暂收尾时间——原始录音已在磁盘，不必等网络慢慢磨。
        """
        if abandon_pending:
            self._abandoned = True
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=2.0 if abandon_pending else None)

    # ── 工作线程 ──────────────────────────────────────
    def _run(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if not self._abandoned:
                    seq, audio = item
                    self._transcribe_one(seq, audio)
            finally:
                self._queue.task_done()

    def _transcribe_one(self, seq, audio):
        net = self.engine.net
        text, cloud_ok = "", False
        if net.is_online() and self.engine.cloud_available:
            try:
                text = self.engine.get_qwen().transcribe(
                    audio, context=self._chunk_context()
                )
                cloud_ok = True
            except Exception:
                errlog.log_exception(f"分段 {seq}：云端转写失败")
                if not net.probe():
                    net.go_offline()
                    self.fell_offline = True
                else:
                    self.cloud_errors += 1
        if not cloud_ok:
            # 云端未成功（离线 / 未配置 / 出错）：本地兜底
            self.status = (
                "🔄 本地转写中…"
                if self.engine.local_loaded()
                else "🔄 加载本地模型（首次约 5s）…"
            )
            try:
                local_text, _ = self.engine.get_local().transcribe(audio)
                text = local_text or ""
            except Exception:
                errlog.log_exception(f"分段 {seq}：本地转写失败")
                with self._lock:
                    self._failed.append(seq)
                self.status = ""
                return
            self.status = ""
        with self._lock:
            self._results[seq] = text

    def _chunk_context(self):
        """每段的 context：基础背景（记忆+文稿）+ 本段已确认的前文。

        前文随录音变长，与文稿共用同一截断上限（取最近窗口）。
        """
        from .qwen_asr import CONTEXT_RUNNING_LABEL

        parts = [self.base_context] if self.base_context else []
        running = self.seed_text + self.text()
        if running:
            if len(running) > ASR_CONTEXT_DOC_LIMIT:
                running = running[-ASR_CONTEXT_DOC_LIMIT:]
            parts.append(CONTEXT_RUNNING_LABEL + "\n" + running)
        return "\n\n".join(parts)
