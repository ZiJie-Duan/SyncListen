# -*- coding: utf-8 -*-
"""音频录制：采集、电平、磁盘流水账、停顿感知分段。

崩溃安全（顺序即优先级）：
1. 每个音频块先进磁盘流水账（.pcm），再交给任何转写/AI 环节。录音中途崩溃，
   流水账仍在磁盘，下次启动由 recover_orphan_pcms() 转成 WAV 找回。
2. 正常停止时流水账原子转成 .wav（临时文件 + os.replace），随后删除流水账。
   流水账写失败（磁盘满等）不影响录音本身——内存 buffer 仍是转写来源，只是
   崩溃时无法从磁盘找回，UI 通过 journal_error 显示「录音未写盘」警告。

写盘为什么不在音频回调里做：回调由 PortAudio 线程驱动，任何阻塞（磁盘卡顿）
都会直接造成输入溢出、丢音频。回调只做内存操作与入队，写盘交给独立线程；
文件以无缓冲方式打开（每块一次 write 直达内核页缓存），进程崩溃零丢失；
按 JOURNAL_SYNC_SECONDS 定期 fsync，把断电时的丢失窗口限制在这么长。

分段（take_chunk）：累计满 CHUNK_TARGET_SECONDS 后，在末尾 CUT_SEARCH_SECONDS
窗口内寻找能量最低的 PAUSE_MIN_SECONDS 时段、从其中点切开。没有"算不算停顿"
的阈值判定：最安静处永远不比整点硬切差。

电平（display_level）：dBFS 刻度——录音软件电平表的通用量程 −60 ~ 0 dBFS——
固定刻度、绝对读数。麦克风没在采集 = 空条；环境噪声 = 底部一小截；说话 = 中上。
"""

import math
import os
import queue
import threading
import time
import wave

import numpy as np
import sounddevice as sd

from ..config import (
    AUDIO_DIR,
    AUDIO_KEEP_COUNT,
    CHUNK_TARGET_SECONDS,
    JOURNAL_SYNC_SECONDS,
    LEVEL_SLICE_SECONDS,
    SAMPLE_RATE,
)

# 切点搜索窗口：切点落在 [目标 − 窗口, 目标] 内。取自然语句间停顿的量级（约 1 秒）：
# 足够容纳一个停顿，又不让段长抖动太大。
CUT_SEARCH_SECONDS = 1.0
# 停顿的最短时长：塞音闭塞段（p/t/k 等发音前的瞬时静音）约 50~150 ms，而语句间
# 停顿 ≥ 200 ms。用 200 ms 能量窗找最安静处，可区分"词内瞬间安静"与"真正的停顿"，
# 避免切在词中间。
PAUSE_MIN_SECONDS = 0.2
# 电平表量程下限（dBFS）。
METER_FLOOR_DBFS = -60.0
# 保留标记：转写失败/中断/异常件改名为 *.keep.wav，prune_wavs 不清理。
KEEP_SUFFIX = ".keep.wav"


def _level_unit(rms):
    """RMS → 电平表量程 [METER_FLOOR_DBFS, 0] 上的位置（0~1）。"""
    if rms <= 0.0:
        return 0.0
    db = 20.0 * math.log10(rms)
    return min(1.0, max(0.0, (db - METER_FLOOR_DBFS) / -METER_FLOOR_DBFS))


def _rms(block):
    """一块音频的均方根电平（float32 → 0~1 量纲浮点）。"""
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


def _to_pcm16(block):
    """float32（多声道取首声道）→ 16-bit PCM mono numpy 数组。"""
    mono = block[:, 0] if getattr(block, "ndim", 1) > 1 else block
    return (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")


def _quietest_point(audio, samplerate):
    """audio 内能量最低的 PAUSE_MIN_SECONDS 窗口的中点样本下标。

    audio 短于一个窗口时返回其长度（无从选择，整点切）。
    """
    n = audio.shape[0]
    win = int(PAUSE_MIN_SECONDS * samplerate)
    if win <= 0 or n <= win:
        return n
    hop = max(1, win // 4)  # 四分之一窗口步进：分辨率 50 ms
    csum = np.concatenate(([0.0], np.cumsum(np.square(audio, dtype=np.float64))))
    starts = np.arange(0, n - win + 1, hop)
    energy = csum[starts + win] - csum[starts]
    return int(starts[int(np.argmin(energy))] + win // 2)


def _cut_point(buf, samplerate):
    """一段待切音频的切点：末尾 CUT_SEARCH_SECONDS 窗口内最安静处。"""
    search = int(CUT_SEARCH_SECONDS * samplerate)
    start = max(0, buf.shape[0] - search)
    return start + _quietest_point(buf[start:], samplerate)


def split_at_pauses(audio, target_seconds=CHUNK_TARGET_SECONDS, samplerate=SAMPLE_RATE):
    """把一整段音频按 take_chunk 同样的规则切成若干段（停顿感知，约 target 秒一段）。

    供事后分段兜底使用（如实时识别中途断开，对尚无定稿的剩余音频分段转写）。
    末段不足 target 秒也单独成段；空音频返回 []。
    """
    if audio is None or audio.size == 0:
        return []
    target = int(target_seconds * samplerate)
    out, pos = [], 0
    while audio.shape[0] - pos > target:
        cut = pos + _cut_point(audio[pos:pos + target], samplerate)
        out.append(audio[pos:cut])
        pos = cut
    if pos < audio.shape[0]:
        out.append(audio[pos:])
    return out


def _write_wav_file(path, pcm_int16, samplerate):
    """16-bit PCM → WAV，写临时文件后原子替换。失败返回 False 且不留半个文件。"""
    tmp = path + ".part"
    try:
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(samplerate)
            wf.writeframes(pcm_int16.tobytes())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _journal_pid(name):
    """从流水账文件名 rec-YYYYmmdd-HHMMSS-<pid>.pcm 解析 pid；解析不出返回 None。"""
    stem = name[:-4]
    tail = stem.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def mark_keep(wav_path):
    """把 WAV 标记为需保留（转写失败/中断/异常件）：改名为 *.keep.wav。

    返回新路径；改名失败返回原路径（文件仍在，只是可能被后续清理）。
    """
    if not wav_path or wav_path.endswith(KEEP_SUFFIX) or not wav_path.endswith(".wav"):
        return wav_path
    new = wav_path[:-4] + KEEP_SUFFIX
    try:
        os.replace(wav_path, new)
        return new
    except OSError:
        return wav_path


def recover_orphan_pcms(audio_dir=AUDIO_DIR, samplerate=SAMPLE_RATE):
    """启动恢复：把上次崩溃遗留的 .pcm 流水账转成 *.keep.wav（不参与清理）。

    返回 [(wav_path, seconds), ...]。转换失败的 .pcm 保留原地，下次再试；
    属于仍在运行的其它实例的流水账跳过（它还在写）。
    """
    out = []
    if not audio_dir:
        return out
    try:
        names = sorted(n for n in os.listdir(audio_dir) if n.endswith(".pcm"))
    except OSError:
        return out
    for name in names:
        pid = _journal_pid(name)
        if pid is not None and pid != os.getpid() and _pid_alive(pid):
            continue
        src = os.path.join(audio_dir, name)
        try:
            pcm = np.fromfile(src, dtype="<i2")
        except OSError:
            continue
        if pcm.size == 0:
            try:
                os.unlink(src)  # 空流水账（开了文件但一个块都没写）
            except OSError:
                pass
            continue
        dst = src[:-4] + KEEP_SUFFIX
        if _write_wav_file(dst, pcm, samplerate):
            try:
                os.unlink(src)
            except OSError:
                pass
            out.append((dst, pcm.size / samplerate))
    return out


def prune_wavs(audio_dir=AUDIO_DIR, keep=AUDIO_KEEP_COUNT):
    """把普通 .wav 数量裁到最近 keep 个（按 mtime 从旧到新删）。0 = 不清理。

    *.keep.wav（失败/中断/崩溃找回件）不参与清理，需用户自行处理。
    """
    if keep <= 0 or not audio_dir:
        return
    try:
        wavs = [
            os.path.join(audio_dir, n)
            for n in os.listdir(audio_dir)
            if n.endswith(".wav") and not n.endswith(KEEP_SUFFIX)
        ]
        wavs.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        return
    for path in wavs[keep:]:
        try:
            os.unlink(path)
        except OSError:
            pass


class _JournalWriter:
    """独立写线程：顺序写入回调送来的 PCM 字节，定期 fsync。

    文件无缓冲打开：每块一次 write 直达内核，进程崩溃零丢失。
    任何写错误记入 error 并停止（不抛到回调线程）。
    """

    def __init__(self, path):
        self.path = path
        self.error = None
        self._q = queue.Queue()
        self._fh = open(path, "wb", buffering=0)  # 打不开由调用方处理
        self._t = threading.Thread(target=self._run, name="audio-journal", daemon=True)
        self._t.start()

    def put(self, data):
        if self.error is None:
            self._q.put(data)

    def close(self):
        """停止写线程并等待已入队数据全部落盘。"""
        self._q.put(None)
        self._t.join()

    def _run(self):
        last_sync = time.monotonic()
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                self._fh.write(item)
                now = time.monotonic()
                if now - last_sync >= JOURNAL_SYNC_SECONDS:
                    os.fsync(self._fh.fileno())
                    last_sync = now
            os.fsync(self._fh.fileno())
        except Exception as e:
            self.error = str(e)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass


class AudioRecorder:
    """sounddevice 录音器：缓存、电平、磁盘流水账、停顿感知分段。"""

    def __init__(self, samplerate=SAMPLE_RATE, channels=1, audio_dir=AUDIO_DIR):
        self.samplerate = samplerate
        self.channels = channels
        self.audio_dir = audio_dir
        self.lock = threading.Lock()
        self.stream = None
        self.dropouts = 0  # PortAudio 报告异常（溢出等）的回调次数，供 UI 提示
        self._on_frame = None
        self._journal = None
        self._journal_error = None
        with self.lock:
            self._reset_buffers()

    def _reset_buffers(self):
        self.frames = []          # 全量块（mono float32，内存兜底）
        self.pending = []         # 尚未被 take_chunk 切走的块
        self.pending_samples = 0
        self._last_rms = 0.0
        # 时间轴：每 LEVEL_SLICE_SECONDS 一格的电平（0~1），供 HUD 画"磁带"
        self.level_track = []
        self._slice_sq = 0.0
        self._slice_n = 0

    # ── 采集 ─────────────────────────────────────────
    def _callback(self, indata, frames, time_info, status):
        if status:
            self.dropouts += 1  # 不在回调里打印：会与主线程重绘抢屏
        mono = indata[:, 0] if indata.ndim > 1 else indata
        blk = mono.copy()  # PortAudio 缓冲仅在回调期间有效，必须复制后再持有
        with self.lock:
            self.frames.append(blk)
            self.pending.append(blk)
            self.pending_samples += blk.shape[0]
            self._last_rms = _rms(blk)
            self._accumulate_track(blk)
        try:
            pcm_bytes = _to_pcm16(blk).tobytes()
        except Exception:
            return
        j = self._journal
        if j is not None:
            j.put(pcm_bytes)
        cb = self._on_frame
        if cb is not None:
            try:
                cb(pcm_bytes)  # 在线流式（Paraformer）：边录边传
            except Exception:
                pass  # 流式发送失败不影响录音，buffer 仍可兜底

    def start(self, on_frame=None):
        """开始录音并打开 .pcm 流水账（先落盘，再谈转写）。

        麦克风打不开（被占用/无权限/无设备）会从这里抛异常，由调用方提示。
        """
        self._on_frame = on_frame
        self.dropouts = 0
        with self.lock:
            self._reset_buffers()
        self._open_journal()
        self.stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="float32",
            callback=self._callback,
        )
        self.stream.start()

    def _open_journal(self):
        self._journal = None
        self._journal_error = None
        if not self.audio_dir:
            return
        try:
            os.makedirs(self.audio_dir, exist_ok=True)
            name = time.strftime("rec-%Y%m%d-%H%M%S") + f"-{os.getpid()}.pcm"
            self._journal = _JournalWriter(os.path.join(self.audio_dir, name))
        except OSError as e:
            self._journal_error = str(e)

    @property
    def journal_error(self):
        """流水账写盘失败原因（None = 正常）。"""
        j = self._journal
        return self._journal_error or (j.error if j is not None else None)

    # ── 电平（供 UI）─────────────────────────────────
    def _accumulate_track(self, blk):
        """把音频块按固定时间片折算成时间轴电平（须持锁调用）。

        逐片累加平方和而不是抽样取值：跨片的块会被切开分别归片，任何一格都是
        该时间片内真实的 RMS，不会因为帧率错过瞬时峰值。
        """
        per = max(1, int(LEVEL_SLICE_SECONDS * self.samplerate))
        i, n = 0, blk.shape[0]
        while i < n:
            take = min(per - self._slice_n, n - i)
            seg = blk[i:i + take]
            self._slice_sq += float(np.dot(seg, seg))
            self._slice_n += take
            i += take
            if self._slice_n >= per:
                self.level_track.append(_level_unit(math.sqrt(self._slice_sq / per)))
                self._slice_sq, self._slice_n = 0.0, 0

    def levels(self):
        """时间轴电平序列的快照（旧→新，每格 LEVEL_SLICE_SECONDS 秒）。"""
        with self.lock:
            return list(self.level_track)

    def level_dbfs(self):
        """最近一块的电平（dBFS）；无信号为 -inf。"""
        with self.lock:
            r = self._last_rms
        return 20.0 * math.log10(r) if r > 0.0 else -math.inf

    def display_level(self):
        """最近电平在电平表量程 [METER_FLOOR_DBFS, 0] 上的位置（0~1）。"""
        db = self.level_dbfs()
        if db == -math.inf:
            return 0.0
        return min(1.0, max(0.0, (db - METER_FLOOR_DBFS) / -METER_FLOOR_DBFS))

    def recent(self, n_samples):
        """最近 n_samples 个样本（供频谱/示波器可视化）；不足则返回已有的全部。"""
        n_samples = max(0, int(n_samples))
        with self.lock:
            got, parts = 0, []
            for blk in reversed(self.frames):
                parts.append(blk)
                got += blk.shape[0]
                if got >= n_samples:
                    break
        if not parts:
            return np.zeros(0, dtype=np.float32)
        parts.reverse()
        buf = np.concatenate(parts)
        return buf[-n_samples:] if n_samples else buf[:0]

    # ── 分段 ─────────────────────────────────────────
    def take_chunk(self, target_seconds=CHUNK_TARGET_SECONDS):
        """累计满 target_seconds 时切出一段，切点取末尾窗口内最安静处。

        返回 float32 mono 音频；不足长返回 None。切走的样本从待处理队列移出，
        全量 frames 保留（stop() 的内存兜底来源）。
        """
        target = int(target_seconds * self.samplerate)
        with self.lock:
            if self.pending_samples < target:
                return None
            buf = np.concatenate(self.pending)
            self.pending = []
            self.pending_samples = 0
        cut = _cut_point(buf, self.samplerate)
        chunk, rest = buf[:cut], buf[cut:]
        if rest.size:
            with self.lock:
                # 回调可能已追加了新块：余量插回队首，保持时间顺序
                self.pending.insert(0, rest)
                self.pending_samples += rest.shape[0]
        return chunk

    def skip_pending(self, n_samples):
        """把待处理队列开头的 n_samples 个样本标记为已处理（不再参与 take_chunk）。

        用于实时识别中途断开后热切换到分段转写：定稿已覆盖的音频不必再转。
        全量 frames 不受影响。
        """
        n = int(n_samples)
        with self.lock:
            while n > 0 and self.pending:
                blk = self.pending[0]
                if blk.shape[0] <= n:
                    n -= blk.shape[0]
                    self.pending_samples -= blk.shape[0]
                    self.pending.pop(0)
                else:
                    self.pending[0] = blk[n:]
                    self.pending_samples -= n
                    n = 0

    # ── 停止与落盘 ────────────────────────────────────
    def stop(self):
        """停止录音，返回 (audio, tail, wav_path)。

        audio：全量音频（整段转写 / 内存兜底）；tail：尚未被 take_chunk 切走的
        尾段（分段转写只需再送这一段）；wav_path：磁盘存档路径——由流水账原子
        生成；流水账不完整则用内存 buffer 生成；都写不出来为 None（audio 仍完整，
        转写照常，.pcm 留在磁盘等下次启动恢复）。一个样本都没录到时 audio/tail
        为 None 并清掉空流水账。
        """
        self._on_frame = None
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        with self.lock:
            audio = np.concatenate(self.frames) if self.frames else None
            tail = np.concatenate(self.pending) if self.pending else None
            self._reset_buffers()
        wav_path = self._finalize_journal(audio)
        return audio, tail, wav_path

    def _finalize_journal(self, fallback_audio):
        j, self._journal = self._journal, None
        have_mem = fallback_audio is not None and fallback_audio.size > 0
        if j is None:
            # 流水账从未建起：退回内存 buffer 直接出 wav（未配置目录则不写）
            if have_mem and self.audio_dir:
                return self._wav_from_array(fallback_audio)
            return None
        j.close()  # 等写线程把队列写完
        pcm_path = j.path
        try:
            pcm = np.fromfile(pcm_path, dtype="<i2")
        except OSError:
            pcm = None
        if j.error is None and pcm is not None and pcm.size > 0:
            source_pcm = pcm
        elif have_mem:
            source_pcm = _to_pcm16(fallback_audio)  # 流水账不完整：内存 buffer 是完整的
        else:
            try:
                os.unlink(pcm_path)
            except OSError:
                pass
            return None
        wav_path = pcm_path[:-4] + ".wav"
        if _write_wav_file(wav_path, source_pcm, self.samplerate):
            try:
                os.unlink(pcm_path)  # 数据已入 wav，流水账功成身退
            except OSError:
                pass
            return wav_path
        # wav 写不出来：留着 .pcm（优先可恢复），下次启动 recover 兜底
        return None

    def _wav_from_array(self, audio):
        if not self.audio_dir:
            return None
        try:
            os.makedirs(self.audio_dir, exist_ok=True)
            path = os.path.join(
                self.audio_dir,
                time.strftime("rec-%Y%m%d-%H%M%S") + f"-{os.getpid()}.wav",
            )
            if _write_wav_file(path, _to_pcm16(audio), self.samplerate):
                return path
        except OSError:
            pass
        return None
