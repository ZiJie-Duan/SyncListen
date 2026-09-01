# -*- coding: utf-8 -*-
"""SyncListen 鲁棒性测试：不依赖真实麦克风与网络。

运行：  python tests/test_robustness.py        （或 python -m pytest tests/）
覆盖：
  · Session 持久化 / 跨实例恢复 / 损坏留证 / 历史上限 / 落盘失败不抛 / 启动空白+Z 找回
  · 录音器：回调复制（PortAudio 缓冲复用）、停顿处切段、尾段返回、流水账→WAV、
    崩溃遗留 .pcm 恢复为 *.keep.wav、清理不动 keep 件、dBFS 电平
  · _capture 整合路径（伪麦克风 + 伪云端/本地）：分段无重复、context 注入、
    云端出错走本地、无云端配置走本地、本地失败标记保留、麦克风打不开不崩、
    Ctrl+C 录音中 = 停止并保留已录
"""

import contextlib
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from synclisten.core import errlog  # noqa: E402
from synclisten.core import recorder as recorder_mod  # noqa: E402
from synclisten.core.network import NetworkManager  # noqa: E402
from synclisten.core.recorder import (  # noqa: E402
    AudioRecorder,
    _quietest_point,
    _to_pcm16,
    mark_keep,
    prune_wavs,
    recover_orphan_pcms,
)
from synclisten.core.session import Session  # noqa: E402
import main as app  # noqa: E402

SR = 16000
_TMP = tempfile.mkdtemp(prefix="sl-test-")
errlog.LOG_DIR = os.path.join(_TMP, "logs")
os.makedirs(errlog.LOG_DIR, exist_ok=True)


# ── 合成音频 ─────────────────────────────────────────

def speech(seconds, amp=0.05, seed=0):
    """类语音的带调制噪声（mono float32）。"""
    rng = np.random.default_rng(seed)
    n = int(round(seconds * SR))
    t = np.arange(n) / SR
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t)  # 3 Hz 音节包络
    return (amp * env * rng.standard_normal(n)).astype(np.float32)


def silence(seconds, amp=0.0005, seed=1):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(int(round(seconds * SR)))).astype(np.float32)


def script_with_pauses(total, pauses):
    """total 秒语音，在 pauses=[(start, end), ...] 处为近静音。"""
    audio = speech(total)
    for a, b in pauses:
        i, j = int(round(a * SR)), int(round(b * SR))
        audio[i:j] = silence((j - i) / SR)[: j - i]
    return audio


def wav_seconds(path):
    with wave.open(path) as wf:
        return wf.getnframes() / wf.getframerate()


def markers(text, prefix=""):
    """解析伪转写标记 <秒数> / L<秒数>。"""
    return [float(m) for m in re.findall(re.escape(prefix) + r"<(\d+\.\d+)>", text)]


# ── Session ──────────────────────────────────────────

def test_session_persist_and_restore():
    d = tempfile.mkdtemp(dir=_TMP)
    p = os.path.join(d, "session.json")
    s = Session(path=p)
    assert s.content == "" and s.history == [""]
    s.commit("一"); s.commit("一\n二"); s.commit("一\n二\n三")
    assert s.undo() and s.content == "一\n二"
    s2 = Session(path=p)  # 下次启动
    assert s2.content == "一\n二" and len(s2.history) == 4 and s2.pos == 2
    assert s2.undo() and s2.content == "一"


def test_session_start_fresh_then_z():
    d = tempfile.mkdtemp(dir=_TMP)
    p = os.path.join(d, "session.json")
    s = Session(path=p); s.commit("上次的内容")
    s2 = Session(path=p)
    assert s2.start_fresh() == len("上次的内容")
    assert s2.content == ""                      # 启动空白
    assert s2.undo() and s2.content == "上次的内容"  # 按 Z 找回
    s2.commit("")                                # 相当于按 [D] 清空后退出
    s3 = Session(path=p)
    n = len(s3.history)
    # 上次退出时已是空白：不再叠加空版本，Z 仍能找回上一份内容
    assert s3.start_fresh() == len("上次的内容") and len(s3.history) == n
    assert Session(path=os.path.join(d, "new.json")).start_fresh() == 0


def test_session_corrupt_file_moved_aside():
    d = tempfile.mkdtemp(dir=_TMP)
    p = os.path.join(d, "session.json")
    with open(p, "w") as f:
        f.write("{corrupted!!")
    s = Session(path=p)
    assert s.load_warning and s.content == ""
    assert any(n.startswith("session.json.corrupt-") for n in os.listdir(d))


def test_session_history_capped():
    from synclisten.core import session as session_mod
    d = tempfile.mkdtemp(dir=_TMP)
    old = session_mod.SESSION_HISTORY_LIMIT
    session_mod.SESSION_HISTORY_LIMIT = 5
    try:
        s = Session(path=os.path.join(d, "s.json"))
        for i in range(20):
            s.commit(str(i))
        assert len(s.history) == 5 and s.content == "19" and s.pos == 4
        assert s.undo() and s.content == "18"
    finally:
        session_mod.SESSION_HISTORY_LIMIT = old


def test_session_save_failure_does_not_raise():
    d = tempfile.mkdtemp(dir=_TMP)
    blocker = os.path.join(d, "not-a-dir")
    open(blocker, "w").close()
    s = Session(path=os.path.join(blocker, "session.json"))  # 父路径是普通文件 → 无法落盘
    s.commit("内容还在内存里")
    assert s.content == "内容还在内存里" and s.save_error


# ── 录音器 ───────────────────────────────────────────

def test_quietest_point_finds_pause_not_edge():
    audio = script_with_pauses(1.0, [(0.5, 0.75)])
    cut = _quietest_point(audio, SR) / SR
    assert 0.55 <= cut <= 0.70, cut


def test_callback_copies_portaudio_buffer():
    """PortAudio 复用同一块缓冲；回调若不复制，切出的段会是被覆写后的垃圾。"""
    rec = AudioRecorder(audio_dir=None)
    buf = np.zeros((int(0.1 * SR), 1), dtype=np.float32)  # 同一个缓冲反复复用
    for i in range(110):  # 11 s
        buf[:] = 0.01 * (i + 1)
        rec._callback(buf, buf.shape[0], None, None)
    chunk = rec.take_chunk(10.0)
    assert chunk is not None
    assert abs(float(chunk[0]) - 0.01) < 1e-6, "首块被后续回调覆写：pending 未复制"
    assert abs(float(chunk[-1]) - float(buf[0, 0])) > 1e-6


def test_take_chunk_cuts_at_pause_and_tail_is_remainder():
    rec = AudioRecorder(audio_dir=None)
    audio = script_with_pauses(10.5, [(9.6, 9.9)])
    blk = int(0.05 * SR)  # 50 ms 块（PortAudio 常见量级）
    for i in range(0, audio.shape[0], blk):
        rec._callback(audio[i:i + blk].reshape(-1, 1), blk, None, None)
    chunk = rec.take_chunk(10.0)
    cut = chunk.shape[0] / SR
    assert 9.6 <= cut <= 9.9, cut
    assert rec.pending_samples == audio.shape[0] - chunk.shape[0]
    assert rec.take_chunk(10.0) is None  # 不足一段
    full, tail, wav = rec.stop()
    assert full.shape[0] == audio.shape[0]
    assert tail.shape[0] == audio.shape[0] - chunk.shape[0]  # 尾段 = 未切走的余量
    assert wav is None  # audio_dir=None：不写盘


def test_journal_to_wav_and_orphan_recovery():
    adir = os.path.join(tempfile.mkdtemp(dir=_TMP), "audio")
    rec = AudioRecorder(audio_dir=adir)
    rec._open_journal()
    assert rec._journal is not None and rec._journal.path.endswith(".pcm")
    for i in range(50):  # 5 s
        rec._callback(speech(0.1, seed=i).reshape(-1, 1), int(0.1 * SR), None, None)
    audio, tail, wav = rec.stop()
    assert wav and wav.endswith(".wav") and not wav.endswith(".keep.wav")
    assert abs(wav_seconds(wav) - 5.0) < 0.01
    assert not any(n.endswith(".pcm") for n in os.listdir(adir))

    # 崩溃遗留：死进程的流水账 → 恢复为 keep 件；活进程（pid 1）的跳过
    dead = os.path.join(adir, "rec-20260101-000000-999999.pcm")
    _to_pcm16(speech(2.5)).tofile(dead)
    live = os.path.join(adir, "rec-20260101-000001-1.pcm")
    _to_pcm16(speech(1.0)).tofile(live)
    out = recover_orphan_pcms(audio_dir=adir)
    assert len(out) == 1 and out[0][0].endswith(".keep.wav") and out[0][1] == 2.5
    assert not os.path.exists(dead) and os.path.exists(live)
    os.unlink(live)

    # 清理：普通件按新旧裁，keep 件不动
    for i in range(3):
        p = os.path.join(adir, f"rec-x{i}.wav")
        shutil.copy(wav, p)
        os.utime(p, (1000 + i, 1000 + i))
    kept = mark_keep(wav)
    assert kept.endswith(".keep.wav") and os.path.exists(kept)
    prune_wavs(audio_dir=adir, keep=1)
    names = sorted(os.listdir(adir))
    assert sum(n.endswith(".keep.wav") for n in names) == 2
    assert sum(n.endswith(".wav") and not n.endswith(".keep.wav") for n in names) == 1


def test_display_level_is_dbfs_scale():
    rec = AudioRecorder(audio_dir=None)
    assert rec.display_level() == 0.0  # 无信号 = 空条（不再是“开局满格”）
    with rec.lock:
        rec._last_rms = 0.1  # -20 dBFS
    assert abs(rec.display_level() - (40 / 60)) < 1e-6
    with rec.lock:
        rec._last_rms = 1e-4  # -80 dBFS，低于量程
    assert rec.display_level() == 0.0


def test_level_track_slices_are_absolute_and_aligned():
    """HUD 时间轴的每一格都是该时间片真实的 RMS：跨块也要按片切开累加。"""
    from synclisten.config import LEVEL_SLICE_SECONDS
    from synclisten.core.recorder import METER_FLOOR_DBFS

    rec = AudioRecorder(audio_dir=None)
    per = int(LEVEL_SLICE_SECONDS * SR)
    # 一片 -20 dBFS 的直流 + 一片静音，切成大小不整除片长的块喂进去
    sig = np.concatenate([np.full(per, 0.1, dtype=np.float32),
                          np.zeros(per, dtype=np.float32)])
    step = per // 3 + 7                       # 故意与片长不整除：块会跨片
    for i in range(0, sig.size, step):
        with rec.lock:
            rec._accumulate_track(sig[i:i + step])
    track = rec.levels()
    assert len(track) == 2, len(track)
    assert abs(track[0] - (40 / 60)) < 1e-3   # -20 dBFS → 量程 2/3 高
    assert track[1] == 0.0                    # 静音就是 0，不做自动增益
    assert METER_FLOOR_DBFS == -60.0


# ── _capture 整合：伪麦克风 + 伪云端/本地 ───────────────

class FakeStream:
    """模拟 sd.InputStream：后台线程按加速时钟把合成音频块喂给回调（复用同一缓冲）。"""
    script = None
    speed = 25.0
    finished = threading.Event()
    delivered = 0
    fail_open = False

    def __init__(self, samplerate, channels, dtype, callback):
        if FakeStream.fail_open:
            raise RuntimeError("Error opening InputStream: Device unavailable")
        self.cb = callback
        self._stop = threading.Event()
        self._t = None

    def start(self):
        FakeStream.finished.clear()
        FakeStream.delivered = 0
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        blk = int(0.1 * SR)
        buf = np.zeros((blk, 1), dtype=np.float32)
        pos, script = 0, FakeStream.script
        while not self._stop.is_set() and pos < script.shape[0]:
            piece = script[pos:pos + blk]
            buf[: piece.shape[0], 0] = piece
            self.cb(buf[: piece.shape[0]], piece.shape[0], None, None)
            pos += piece.shape[0]
            FakeStream.delivered = pos
            time.sleep(0.1 / FakeStream.speed)
        FakeStream.finished.set()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join()

    def close(self):
        pass


class FakeQwen:
    def __init__(self, fail_calls=()):
        self.calls = []
        self.fail_calls = set(fail_calls)

    def transcribe(self, audio, context=""):
        idx = len(self.calls)
        self.calls.append((audio.shape[0] / SR, context))
        if idx in self.fail_calls:
            raise RuntimeError("HTTP 500")
        return f"<{audio.shape[0] / SR:.2f}>"


class FakeLocal:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        if self.fail:
            raise RuntimeError("funasr not installed")
        return f"L<{audio.shape[0] / SR:.2f}>", None


class FakeEngine:
    def __init__(self, recorder, online=True, cloud_available=True,
                 qwen_fail=(), local_fail=False, probe=True):
        self.recorder = recorder
        self.net = NetworkManager(NetworkManager.ONLINE if online else NetworkManager.OFFLINE)
        self.net.probe = lambda timeout=None: probe
        self.cloud_available = cloud_available
        self.online_engine = "qwen"
        self.qwen = FakeQwen(qwen_fail)
        self.local = FakeLocal(local_fail)
        self._local_loaded = False

    def get_qwen(self):
        return self.qwen

    def get_local(self):
        self._local_loaded = True
        return self.local

    def local_loaded(self):
        return self._local_loaded


@contextlib.contextmanager
def harness(script, poll=None):
    """装配伪环境：伪麦克风、TTY、按键轮询、无操作重绘；产出 (session, adir, frames)。"""
    d = tempfile.mkdtemp(dir=_TMP)
    adir = os.path.join(d, "audio")
    frames = []
    FakeStream.script = script
    FakeStream.fail_open = False

    def default_poll(timeout=0.1):
        if FakeStream.finished.is_set():
            return "\n"
        time.sleep(0.002)
        return None

    saved = (recorder_mod.sd, app._is_tty, app._poll_key, app._cbreak,
             app.redraw, app.copy_to_clipboard, app.CHUNK_TARGET_SECONDS)
    recorder_mod.sd = types.SimpleNamespace(InputStream=FakeStream)
    app.CHUNK_TARGET_SECONDS = 10.0  # 测试脚本按 10 秒切点设计
    app._is_tty = lambda: True
    app._poll_key = poll or default_poll
    app._cbreak = contextlib.nullcontext
    app.redraw = lambda *a, **k: frames.append(k.get("transient", ""))
    app.copy_to_clipboard = lambda text: None
    try:
        yield Session(path=os.path.join(d, "session.json")), adir, frames
    finally:
        (recorder_mod.sd, app._is_tty, app._poll_key, app._cbreak,
         app.redraw, app.copy_to_clipboard, app.CHUNK_TARGET_SECONDS) = saved


def test_capture_chunks_no_duplication_and_context():
    script = script_with_pauses(25.0, [(9.5, 9.8), (19.4, 19.7)])
    with harness(script) as (session, adir, frames):
        rec = AudioRecorder(audio_dir=adir)
        eng = FakeEngine(rec)
        text, note, wav = app._capture(session, eng, "rec")
    secs = markers(text)
    assert len(secs) == 3, (text, note)                    # 两个中途段 + 一个尾段
    assert abs(sum(secs) - 25.0) < 0.05, secs               # 无重复、无丢失
    assert 9.5 <= secs[0] <= 9.8, secs                      # 切在第一个停顿
    assert 19.4 <= secs[0] + secs[1] <= 19.7, secs          # 切在第二个停顿
    assert wav and abs(wav_seconds(wav) - 25.0) < 0.01 and not wav.endswith(".keep.wav")
    assert note == ""
    # context 注入：第 2 段的 context 带有第 1 段已识别文本
    assert "本段前文" in eng.qwen.calls[1][1] and f"<{secs[0]:.2f}>" in eng.qwen.calls[1][1]
    # 实时上屏：录音期间至少有一帧显示了已识别文本
    assert any(f"<{secs[0]:.2f}>" in f for f in frames)


def test_capture_cloud_error_falls_back_to_local_per_chunk():
    script = script_with_pauses(25.0, [(9.5, 9.8), (19.4, 19.7)])
    with harness(script) as (session, adir, frames):
        eng = FakeEngine(AudioRecorder(audio_dir=adir), qwen_fail={1})
        text, note, wav = app._capture(session, eng, "rec")
    all_m, local = markers(text), markers(text, "L")  # 通配正则也会匹配 L<…>，故 all 含 local
    assert len(all_m) == 3 and len(local) == 1, (text, note)
    assert abs(sum(all_m) - 25.0) < 0.05
    assert len(eng.qwen.calls) == 3 and eng.local.calls == 1
    assert "云端出错" in note and "识别失败" not in note
    assert eng.net.is_online()                              # 探测正常 → 不切离线
    assert not wav.endswith(".keep.wav")                    # 没有失败段 → 普通件


def test_capture_no_cloud_key_uses_local():
    with harness(script_with_pauses(12.0, [(9.5, 9.8)])) as (session, adir, frames):
        eng = FakeEngine(AudioRecorder(audio_dir=adir), cloud_available=False)
        text, note, wav = app._capture(session, eng, "rec")
    assert len(markers(text, "L")) == 2 and abs(sum(markers(text, "L")) - 12.0) < 0.05
    assert eng.qwen.calls == []


def test_capture_local_failure_marks_keep():
    with harness(speech(3.0)) as (session, adir, frames):
        eng = FakeEngine(AudioRecorder(audio_dir=adir), online=False, local_fail=True, probe=False)
        text, note, wav = app._capture(session, eng, "rec")
    assert text == "" and "1 段识别失败" in note
    assert wav.endswith(".keep.wav") and os.path.exists(wav)
    assert "原始录音已保留" in note


def test_capture_mic_open_failure_does_not_crash():
    with harness(speech(1.0)) as (session, adir, frames):
        FakeStream.fail_open = True
        eng = FakeEngine(AudioRecorder(audio_dir=adir))
        text, note, wav = app._capture(session, eng, "rec")
    assert text is None and note.startswith("✕") and "Device unavailable" in note
    assert wav is None
    assert not os.path.exists(adir) or not os.listdir(adir)  # 空流水账已清理


def test_capture_ctrl_c_while_recording_stops_and_keeps():
    fired = []

    def poll(timeout=0.1):
        if FakeStream.delivered >= 12 * SR and not fired:
            fired.append(1)          # 一次按键只触发一次
            raise KeyboardInterrupt
        time.sleep(0.002)
        return None

    with harness(speech(30.0), poll=poll) as (session, adir, frames):
        eng = FakeEngine(AudioRecorder(audio_dir=adir))
        text, note, wav = app._capture(session, eng, "rec")
    total = sum(markers(text))
    assert 12.0 <= total <= 13.0, (total, note)             # 停在 12 s 附近，已录部分全转写
    assert abs(wav_seconds(wav) - total) < 0.05
    assert note == "" and not wav.endswith(".keep.wav")     # 录音中 Ctrl+C 不算中断收尾
    assert app._QUIT_REQUESTED is False                     # Ctrl+C ≠ 退出


# ── 运行器 ───────────────────────────────────────────

if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"✅ {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"❌ {name}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)
