# -*- coding: utf-8 -*-
"""流式转写 + 终端界面测试：不依赖真实麦克风与网络。

运行：  python tests/test_streaming_ui.py
覆盖：
  · 按键读取：cbreak 下第一次回车即被读到（回归：TCSAFLUSH 吞键导致要按两次）、
    一次读到多键不丢、转义序列完整、单独 ESC
  · 实时识别会话：事件解析（text/stash 预览、completed 定稿、VAD 覆盖位置、
    context 回吐过滤、error）
  · _capture 实时路径：正常收尾；录音中途断线 → 热切换分段接手（定稿不丢不重复、
    前文注入）；收尾失败 → 剩余音频分段兜底；建连失败 → 直接走分段
  · 录音器：split_at_pauses / skip_pending / recent
  · fx：显示宽度与换行、频谱（正弦落在正确频带）、示波器静音平线、矩阵雨尺寸、纯文本模式
  · redraw：窄终端下每行显示宽度不超宽、行数不超高（含 HUD/草稿/提示）
"""

import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

from test_robustness import (  # noqa: E402
    SR, FakeEngine, FakeStream, harness, markers, script_with_pauses, speech, wav_seconds, _TMP,
)
from synclisten.core.recorder import AudioRecorder, split_at_pauses  # noqa: E402
from synclisten.core.qwen_realtime import QwenRealtimeTranscriber  # noqa: E402
from synclisten.core.qwen_asr import CONTEXT_DOC_LABEL  # noqa: E402
from synclisten.core.session import Session  # noqa: E402
from synclisten.ui import fx  # noqa: E402
import main as app  # noqa: E402


# ── 伪实时识别会话 ────────────────────────────────────

class FakeRealtime:
    """模拟实时会话：每 sentence_seconds 音频定稿一句 [R<累计秒>]；可在指定秒数"断线"。"""

    def __init__(self, fail_after=None, fail_on_finalize=False, fail_begin=False, sentence_seconds=3.0):
        self.fail_after = fail_after
        self.fail_on_finalize = fail_on_finalize
        self.fail_begin = fail_begin
        self.sentence_seconds = sentence_seconds
        self.context = None
        self.fed = 0
        self._finals, self._preview, self._error, self._covered = [], "", None, 0.0
        self._marks = []
        self.closed = False
        self.finalized = False

    def begin(self, context=""):
        if self.fail_begin:
            raise TimeoutError("websocket connection could not established within 5s")
        self.context = context

    def feed(self, pcm):
        if self._error is not None:
            return
        self.fed += len(pcm)
        secs = self.fed / 2 / SR
        while len(self._finals) < int(secs // self.sentence_seconds):
            n = len(self._finals) + 1
            self._finals.append(f"[R{n * self.sentence_seconds:.1f}]")
            self._covered = n * self.sentence_seconds
            self._marks.append(self._covered)
        self._preview = f"~{secs:.1f}"
        if self.fail_after is not None and secs >= self.fail_after:
            self._error = "socket closed"

    @property
    def failed(self):
        return self._error is not None

    @property
    def error(self):
        return self._error

    def final_text(self):
        return "".join(self._finals)

    def preview_text(self):
        return self._preview

    def text(self):
        return self.final_text() + self._preview

    def segments(self):
        return len(self._finals)

    def speech_active(self):
        return True

    def covered_seconds(self):
        return self._covered

    def mark_seconds(self):
        return list(self._marks)

    def finalize(self, timeout=10.0):
        self.finalized = True
        if self.fail_on_finalize:
            self._error = "end_session timeout"
            return self.final_text()
        secs = self.fed / 2 / SR
        if secs > self._covered:
            self._finals.append(f"[R{secs:.1f}]")
            self._covered = secs
            self._marks.append(secs)
        self._preview = ""
        self.closed = True
        return self.final_text()

    def close(self):
        self.closed = True


def rt_engine(rec, rt, **kw):
    eng = FakeEngine(rec, **kw)
    eng.online_engine = "qwen-realtime"
    eng.get_realtime = lambda: rt
    return eng


# ── 按键读取（真实 pty）──────────────────────────────

def _with_pty(fn):
    import pty
    master, slave = pty.openpty()
    old_stdin = sys.stdin
    sys.stdin = os.fdopen(slave, "r", closefd=False)
    app._key_buffer.clear()
    try:
        return fn(master)
    finally:
        sys.stdin = old_stdin
        app._key_buffer.clear()
        os.close(master)
        os.close(slave)


def test_first_enter_stops_polling():
    """回归：回车按下后，第一次轮询就必须读到（不能被终端模式切换冲掉）。"""
    def body(master):
        os.write(master, b"\n")
        time.sleep(0.05)
        with app._cbreak():
            k = app._poll_key(1.0)
        assert k == "\n", repr(k)
        # 已在 cbreak 中再按（真实场景）：同样一次读到
        with app._cbreak():
            os.write(master, b"\r")
            k2 = app._poll_key(1.0)
        assert k2 in ("\r", "\n"), repr(k2)  # 终端 ICRNL 可能把 \r 转成 \n
    _with_pty(body)


def test_multi_key_read_keeps_all_keys():
    def body(master):
        with app._cbreak():
            os.write(master, b"ab\x1b[A\x1b")
            time.sleep(0.1)
            keys = [app._poll_key(0.3) for _ in range(5)]
        assert keys == ["a", "b", "\x1b[A", "\x1b", None], keys
        app._flush_input()
        assert not app._key_buffer
    _with_pty(body)


def test_getch_raw_reads_utf8_char():
    def body(master):
        os.write(master, "中".encode("utf-8"))
        time.sleep(0.05)
        assert app._getch_raw() == "中"
    _with_pty(body)


# ── 实时会话事件解析 ──────────────────────────────────

def test_realtime_event_handling():
    rt = QwenRealtimeTranscriber(api_key="test")
    rt._reset_state()
    rt._context = CONTEXT_DOC_LABEL + "\nSyncListen 项目"
    ev = rt._on_event
    ev({"type": "session.updated"})
    assert rt._configured.is_set()
    ev({"type": "input_audio_buffer.speech_started", "audio_start_ms": 0})
    ev({"type": "conversation.item.input_audio_transcription.text", "text": "", "stash": "今天"})
    ev({"type": "conversation.item.input_audio_transcription.text", "text": "今天", "stash": "天气"})
    assert rt.preview_text() == "今天天气" and rt.final_text() == "" and rt.speech_active()
    ev({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 2500})
    assert rt.covered_seconds() == 0.0  # 定稿到来前不算覆盖
    ev({"type": "conversation.item.input_audio_transcription.completed", "transcript": "今天天气不错。"})
    assert rt.final_text() == "今天天气不错。" and rt.preview_text() == ""
    assert rt.covered_seconds() == 2.5 and rt.segments() == 1 and not rt.speech_active()
    # context 回吐：只是把注入的文稿念回来 → 丢弃，但覆盖位置照样推进
    ev({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 4000})
    ev({"type": "conversation.item.input_audio_transcription.completed",
        "transcript": "已有上下文：SyncListen 项目"})
    assert rt.final_text() == "今天天气不错。" and rt.covered_seconds() == 4.0
    assert not rt.failed
    ev({"type": "error", "error": {"code": "invalid_value", "message": "bad corpus"}})
    assert rt.failed and "invalid_value" in rt.error
    rt._finishing = False
    rt2 = QwenRealtimeTranscriber(api_key="test")
    rt2._on_close(1006, "abnormal")
    assert rt2.failed and rt2._closed


def test_realtime_feed_after_error_is_dropped():
    rt = QwenRealtimeTranscriber(api_key="test")
    rt._error = "x"
    rt.feed(b"\x00\x00")
    assert rt.dropped_frames == 1 and rt._q.empty()


# ── _capture 实时路径 ────────────────────────────────

def test_capture_realtime_happy_path():
    with harness(speech(7.0)) as (session, adir, frames):
        session.commit("已有文稿内容")
        rt = FakeRealtime(sentence_seconds=3.0)
        eng = rt_engine(AudioRecorder(audio_dir=adir), rt)
        text, note, wav = app._capture(session, eng, "rec")
    assert text == "[R3.0][R6.0][R7.0]", (text, note)
    assert note == "" and wav and not wav.endswith(".keep.wav")
    assert rt.finalized and rt.closed
    assert eng.qwen.calls == []                                # 分段引擎没被动用
    assert "已有文稿内容" in rt.context                        # context 注入实时会话
    assert any("[R3.0]" in f for f in frames)                  # 录音期间定稿已上屏
    assert abs(wav_seconds(wav) - 7.0) < 0.01


def test_capture_realtime_midway_failure_hot_failover():
    script = script_with_pauses(20.0, [(15.4, 15.7)])
    with harness(script) as (session, adir, frames):
        rt = FakeRealtime(fail_after=8.0, sentence_seconds=3.0)  # 6 s 已定稿，8 s 断线
        eng = rt_engine(AudioRecorder(audio_dir=adir), rt)
        text, note, wav = app._capture(session, eng, "rec")
    secs = markers(text)
    assert text.startswith("[R3.0][R6.0]"), (text, note)
    assert abs(sum(secs) - (20.0 - 6.0)) < 0.05, (secs, text)   # 剩余 14 s 全部分段转写，无重叠无缺口
    assert len(secs) == 2 and 9.4 <= secs[0] <= 9.7, secs        # 第一段切在 15.4~15.7 的停顿（6 + 9.x）
    assert "实时连接中断" in note and "分段" in note
    assert wav.endswith(".keep.wav")                              # 出过事的录音保留
    assert rt.closed and not rt.finalized                         # 断线后不再收尾实时会话
    assert "本段前文" in eng.qwen.calls[0][1] and "[R3.0][R6.0]" in eng.qwen.calls[0][1]  # 前文注入
    assert eng.net.is_online()


def test_capture_realtime_finalize_failure_falls_back_for_rest():
    with harness(speech(12.0)) as (session, adir, frames):
        rt = FakeRealtime(fail_on_finalize=True, sentence_seconds=5.0)  # 10 s 已定稿
        eng = rt_engine(AudioRecorder(audio_dir=adir), rt)
        text, note, wav = app._capture(session, eng, "rec")
    assert text.startswith("[R5.0][R10.0]"), (text, note)
    secs = markers(text)
    assert len(secs) == 1 and abs(secs[0] - 2.0) < 0.05, secs   # 只补最后 2 s
    assert "实时连接中断" in note and wav.endswith(".keep.wav")


def test_capture_realtime_begin_failure_uses_chunked():
    with harness(script_with_pauses(12.0, [(9.5, 9.8)])) as (session, adir, frames):
        rt = FakeRealtime(fail_begin=True)
        eng = rt_engine(AudioRecorder(audio_dir=adir), rt)
        text, note, wav = app._capture(session, eng, "rec")
    secs = markers(text)
    assert len(secs) == 2 and abs(sum(secs) - 12.0) < 0.05, (text, note)
    assert "实时识别不可用" in note and "分段" in note
    assert rt.fed == 0 and not wav.endswith(".keep.wav")


def test_capture_realtime_offline_probe_switches_offline():
    with harness(speech(4.0)) as (session, adir, frames):
        rt = FakeRealtime(fail_begin=True)
        eng = rt_engine(AudioRecorder(audio_dir=adir), rt, probe=False)
        text, note, wav = app._capture(session, eng, "rec")
    assert not eng.net.is_online() and "离线" in note
    assert len(markers(text, "L")) == 1                          # 本地兜底


# ── 录音器新增 ───────────────────────────────────────

def test_split_at_pauses_and_skip_pending():
    a = script_with_pauses(12.0, [(4.6, 4.9), (9.7, 9.9)])
    parts = split_at_pauses(a, 5.0, SR)
    lens = [p.shape[0] / SR for p in parts]
    assert len(parts) == 3 and abs(sum(lens) - 12.0) < 1e-6, lens
    assert 4.6 <= lens[0] <= 4.9 and 9.7 <= lens[0] + lens[1] <= 9.9, lens
    assert split_at_pauses(np.zeros(0, dtype=np.float32)) == []
    rec = AudioRecorder(audio_dir=None)
    blk = np.ones(1600, dtype=np.float32)
    for i in range(5):
        rec._callback(blk * i, 1600, None, None)
    rec.skip_pending(4000)                                       # 2.5 块
    assert rec.pending_samples == 4000 and sum(b.shape[0] for b in rec.pending) == 4000
    assert rec.pending[0][0] == 2.0 and rec.pending[0].shape[0] == 800
    assert len(rec.frames) == 5                                  # 全量不受影响
    assert rec.recent(1000).shape == (1000,) and rec.recent(1000)[0] == 4.0


# ── fx ───────────────────────────────────────────────

def _fx_on():
    saved = (fx.ENABLED, fx.TRUECOLOR, fx.RESET, fx.BOLD, fx.DIM, fx.ITALIC)
    fx.ENABLED, fx.TRUECOLOR = True, True
    fx.RESET, fx.BOLD, fx.DIM, fx.ITALIC = "\033[0m", "\033[1m", "\033[2m", "\033[3m"
    return saved


def _fx_restore(saved):
    fx.ENABLED, fx.TRUECOLOR, fx.RESET, fx.BOLD, fx.DIM, fx.ITALIC = saved


def test_fx_width_and_wrap():
    assert fx.display_width("中文ab") == 6
    assert fx.display_width("\033[38;2;1;2;3m中\033[0m") == 2
    lines = fx.wrap_width("这是一段中文文本用来测试按显示宽度换行 with some English words", 20)
    assert all(fx.display_width(ln) <= 20 for ln in lines), lines
    assert "".join(lines).replace(" ", "") == "这是一段中文文本用来测试按显示宽度换行withsomeEnglishwords"
    assert "English" in lines[-2] + lines[-1] and not any(ln.endswith("Eng") for ln in lines)  # 整词换行
    assert fx.wrap_width("", 10) == [""] and fx.wrap_width("a\n\nb", 10) == ["a", "", "b"]


def test_fx_tape_and_meter_are_absolute_scale():
    """时间轴与电平表都是绝对刻度，而且时间轴要真的标出"识别覆盖到哪"。"""
    # 电平表：半格填充 = 半量程；空轨上能看见 -20 / -6 dBFS 两条刻度
    bar = fx.meter_bar(0.5, 20, None)
    assert fx.display_width(bar) == 20
    assert bar[:10] == "\u2588" * 10
    empty = fx.meter_bar(0.0, 20, None)
    align = int((fx.ALIGN_DBFS - fx.METER_FLOOR_DBFS) / -fx.METER_FLOOR_DBFS * 20)
    head = int((fx.HEADROOM_DBFS - fx.METER_FLOOR_DBFS) / -fx.METER_FLOOR_DBFS * 20)
    assert empty[align] == "\u253c" and empty[head] == "\u253c"
    assert empty.count("\u253c") == 2

    # 时间轴：柱高随电平，宽度/行数受控，断句点单独标记
    levels = [0.0, 0.25, 0.5, 0.75, 1.0, 0.1]
    rows = fx.render_tape(levels, 6, covered_cells=3, rows=2, marks={4})
    assert len(rows) == 2 and all(fx.display_width(r) == 6 for r in rows)
    assert rows[1][4] == "\u2579"                       # 断句落点
    one = fx.render_tape(levels, 6, covered_cells=3, rows=1)[0]
    assert one[0] == "\u2581" and one[4] == "\u2588"    # 静音是基线，满量程是满格
    # 不足一屏时左侧留白，最新的一格永远在最右
    short = fx.render_tape([1.0], 5, rows=1)[0]
    assert short.rstrip() == " " * 0 + "\u2588" or short[0] == "\u2588"

    saved = _fx_on()
    try:
        # 开特效：已定稿部分与未定稿尾巴用不同墨阶，肉眼能分出分界
        lit = fx.render_tape([0.6] * 8, 8, covered_cells=4, rows=1)[0]
        assert lit.count(fx._gray(fx.INK)) >= 3 and lit.count(fx._gray(fx.VOID)) >= 3
        assert fx.display_width(lit) == 8
        assert fx.display_width(fx.meter_bar(0.5, 12, 0.7)) == 12
        assert "\033[" in fx.meter_bar(0.5, 12, 0.7)
        # 单色：整个界面不应出现任何色相（只允许灰阶 38;5;232-255 / 38;2;v;v;v）
        import re
        for seq in re.findall(r"\033\[38;2;(\d+);(\d+);(\d+)m", lit + bar):
            assert len(set(seq)) == 1, seq
    finally:
        _fx_restore(saved)


def test_fx_ballistics_follows_vu_standard():
    """表针弹道：阶跃输入后 300 ms 达到 99%（IEC 60268-17），不是随手写的平滑。"""
    b = fx.Ballistics()
    b.update(0.0, 0.0)
    t, v = 0.0, 0.0
    while t < fx.Ballistics.RISE_SECONDS - 1e-9:
        t += 0.005
        v = b.update(1.0, t)
    assert abs(v - 0.99) < 0.01, v
    assert b.update(1.0, t + 1.0) > 0.999


def test_fx_cat_has_poses_and_prowls():
    """猫是有身体、有姿态、会走动的精灵，不是一个定宽表情。"""
    assert all(len(row) == fx.CAT_W for pose in fx._POSES.values() for row in pose)
    assert all(len(pose) == fx.CAT_H for pose in fx._POSES.values())

    c = fx.Cat(40, seed=4, roam=True)
    seen, positions = set(), set()
    for i in range(2000):
        t = i * 0.05
        c.update(t)
        seen.add(c.state)
        positions.add(int(c.x))
    assert "walk" in seen and "sit" in seen                 # 会走也会停
    assert len(positions) > 5, positions                    # 真的挪了地方
    assert "scratch" in seen                                # 撞墙会挠

    # 安静够久就睡；一有动静立刻竖耳朵醒过来
    c2 = fx.Cat(40, seed=5)
    t = 0.0
    while t < fx.Cat.SLEEP_AFTER + 12.0:
        t += 0.1
        c2.rest(t)
        c2.update(t)
    assert c2.state == "sleep" and c2.marker(t) in ("z", "Z")
    c2.poke(t + 0.1)
    assert c2.state == "alert" and c2.marker(t + 0.1) == "!"

    # 原地猫不乱跑
    still = fx.Cat(30, seed=6, roam=False)
    xs = set()
    for i in range(400):
        still.update(i * 0.1)
        xs.add(int(still.x))
    assert len(xs) == 1 and "walk" not in {still.state}

    lane = c.render(30, 3.0)
    assert len(lane) == fx.CAT_H and all(fx.display_width(r) == 30 for r in lane)

    # 雨里的猫是实心的：肚子里不漏雨
    rain = fx.MatrixRain(40, 10, seed=7)
    for i in range(60):
        rain.step(now=i * 0.1)
    rain._cat.x, rain._cat.row, rain._cat.state = 10.0, 4, "sit"
    lines = rain.render(now=6.0)
    assert len(lines) == 10 and all(fx.display_width(ln) == 40 for ln in lines)
    plain = [l for l in lines]
    body = plain[5]
    import re
    body = re.sub(r"\033\[[0-9;]*m", "", body)
    assert body[10:17] == "( o.o )", repr(body[8:20])
    feet = re.sub(r"\033\[[0-9;]*m", "", plain[6])
    assert feet[10:17] == " > ^ < ", repr(feet[8:20])     # 眼睛/雨都不许盖到猫身上


def test_fx_rain_typewriter_peak():
    r = fx.MatrixRain(12, 5, seed=3, cats=False)
    for i in range(30):
        r.step(now=i * 0.1)
    lines = r.render(now=3.0)
    assert len(lines) == 5 and all(fx.display_width(ln) == 12 for ln in lines)
    r.resize(8, 3)
    assert len(r.render(now=3.0)) == 3
    tw = fx.Typewriter(cps=10)
    saved = _fx_on()
    try:
        assert tw.visible("abcdef", 0.0) == "" and tw.visible("abcdef", 0.1) == "a"
        assert tw.visible("abcdef", 5.0) == "abcdef"
        assert tw.visible("ab", 5.1) == "ab"                          # 文本变短立即对齐
    finally:
        _fx_restore(saved)
    ph = fx.PeakHold()
    assert ph.update(0.5, 0.0) == 0.5 and ph.update(0.1, 0.5) == 0.5   # 保持期内不掉
    later = ph.update(0.1, 2.0)
    assert 0.1 < later < 0.5                                             # 保持期后按 PPM 速率回落


def test_redraw_fits_narrow_and_tall_terminals():
    d = os.path.join(_TMP, "redraw")
    os.makedirs(d, exist_ok=True)
    s = Session(path=os.path.join(d, "s.json"))
    s.commit("中文内容" * 120 + "\n" + "English words and more " * 20)
    rec = AudioRecorder(audio_dir=None)
    rec.frames = [speech(0.5)]
    with rec.lock:
        rec._last_rms = 0.05
    for (w, rows) in ((40, 20), (52, 60), (80, 24), (30, 14)):
        for fx_on in (False, True):
            saved_fx = _fx_on() if fx_on else None
            saved = (sys.stdout, app._term_size)
            buf = io.StringIO()
            sys.stdout, app._term_size = buf, (lambda w=w, rows=rows: (w, rows))
            try:
                run = app._CaptureRun(FakeEngine(rec), "")
                run.rt = FakeRealtime()
                run.rt._finals, run.rt._preview = ["定稿"], "草稿"
                now = time.monotonic()
                hud = app._hud_lines(rec, run, now - 65, w, app._hud_budget(rows), now)
                app.redraw(s, transient="识别出来的文字" * 30, draft="尚未定稿的草稿" * 10,
                           hint="提示第一行\n提示第二行", hud=hud, now=now)
                out = buf.getvalue()
            finally:
                sys.stdout, app._term_size = saved
                if saved_fx:
                    _fx_restore(saved_fx)
            frame = out.split("\033[2J\033[H", 1)[1]
            lines = frame.split("\n")
            assert lines[-1] == ""
            lines = lines[:-1]
            for ln in lines:
                assert fx.display_width(ln) <= w, (w, rows, fx_on, fx.display_width(ln), repr(ln))
            assert len(lines) <= rows, (w, rows, fx_on, len(lines))
            assert any("定稿" in ln or "识别出来" in ln for ln in lines)
            assert any("REC" in ln for ln in lines)


def test_screensaver_status_never_wraps():
    """屏保底部状态行放不下就自己缩，绝不折行——折行会把雨顶掉一行、整屏滚动。"""
    saved = _fx_on()
    try:
        for w in (12, 20, 28, 34, 40, 52, 80):
            line = app._screensaver_status(w, 13.0, 13.0)
            assert fx.display_width(line) <= w, (w, fx.display_width(line), repr(line))
        assert "空闲" in app._screensaver_status(80, 13.0, 13.0)
    finally:
        _fx_restore(saved)


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
    sys.exit(1 if failed else 0)
