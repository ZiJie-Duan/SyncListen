# -*- coding: utf-8 -*-
"""终端视觉：单色墨阶 + 物理化动效 + 猫。

设计取向：**只有明暗，没有色相**。层次靠灰阶（墨深浅）、字重、留白和细线，
动效靠物理量（表头弹道、峰值回落、掠光）而不是彩虹渐变——要的是质感和动感，
不是花哨。

纯 ANSI + numpy，无第三方 UI 依赖；所有函数只生成字符串、不做 I/O。

开关：SYNCLISTEN_FX=0 或 NO_COLOR 非空 → 关闭样式与动画（函数照常可调用，
退化为纯文本）。COLORTERM=truecolor/24bit 用 24 位灰阶，否则用 256 色灰阶轨
（232–255 共 24 级）。

量纲原则：所有音频可视化都用固定的 dBFS 刻度 [METER_FLOOR_DBFS, 0]，不做自动
增益——安静就是矮，说话就是高，读数有绝对意义；刻度线画在广播惯用的对齐电平
（-20 dBFS）与留空电平（-6 dBFS）上。
"""

import math
import os
import random
import sys
import time
import unicodedata

import numpy as np

# ── 开关与能力探测 ────────────────────────────────────

def _detect_enabled():
    if os.environ.get("SYNCLISTEN_FX", "1").strip() in ("0", "false", "no", "off"):
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


ENABLED = _detect_enabled()
TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")

RESET = "\033[0m" if ENABLED else ""
BOLD = "\033[1m" if ENABLED else ""
DIM = "\033[2m" if ENABLED else ""
ITALIC = "\033[3m" if ENABLED else ""
REVERSE = "\033[7m" if ENABLED else ""

# 电平量程下限（dBFS），与 recorder.METER_FLOOR_DBFS 一致
METER_FLOOR_DBFS = -60.0
# 刻度：广播对齐电平与数字留空电平（在 [-60,0] 量程上的归一化位置）
ALIGN_DBFS = -20.0
HEADROOM_DBFS = -6.0

# ── 墨阶（唯一的“调色板”：灰度 0–255）────────────────

VOID = 60      # 几乎隐没：未到达的刻度、背景纹理
FAINT = 96     # 细线、边框、失活项
MUTED = 140    # 次要文字
INK = 198      # 正文
HI = 245       # 强调：按键、当前值、掠光峰

def _gray(v):
    """灰阶转义码。v ∈ [0,255]。"""
    v = int(min(255, max(0, v)))
    if TRUECOLOR:
        return f"\033[38;2;{v};{v};{v}m"
    if v < 8:
        return "\033[38;5;16m"
    if v > 248:
        return "\033[38;5;231m"
    return f"\033[38;5;{232 + int(round((v - 8) / 247 * 23))}m"


def fg(level):
    if not ENABLED or level is None:
        return ""
    return _gray(level)


def paint(text, level=None, bold=False, dim=False, italic=False, reverse=False):
    """给文本上墨/加样式；关闭特效时原样返回。"""
    if not ENABLED or not text:
        return text
    pre = (fg(level) + (BOLD if bold else "") + (DIM if dim else "")
           + (ITALIC if italic else "") + (REVERSE if reverse else ""))
    return f"{pre}{text}{RESET}" if pre else text


def alert(text, strong=False):
    """告警：单色里用字重和前缀符号区分轻重，不用颜色。"""
    return paint(text, HI, bold=True) if strong else paint(text, INK, bold=True)


# ── 掠光：单色的“流光”，一束高光沿文本扫过 ─────────────

def _highlight(i, n, pos, base, peak, sigma=0.14):
    """第 i 个字符在高光位置 pos（0~1）下的亮度（高斯衰减，环绕）。"""
    d = (i / max(1, n - 1)) - pos
    d -= round(d)  # 环绕到 [-0.5, 0.5]
    return base + (peak - base) * math.exp(-(d / sigma) ** 2)


def sweep(text, now, speed=0.35, base=MUTED, peak=HI, bold=False, sigma=0.14):
    """掠光文字：一束高光沿文本循环扫过（等待/标题用）。"""
    if not ENABLED or not text:
        return text
    n = len(text)
    pos = (now * speed) % 1.0
    out = [(BOLD if bold else "")]
    for i, ch in enumerate(text):
        out.append(_gray(_highlight(i, n, pos, base, peak, sigma)) + ch)
    out.append(RESET)
    return "".join(out)


def trail(text, tail=6, level=INK, peak=HI):
    """末尾若干字符更亮的拖影：文字像刚落笔一样还“热”着。"""
    if not ENABLED or not text:
        return text
    n = len(text)
    head = text[: max(0, n - tail)]
    out = [fg(level) + head] if head else [fg(level)]
    for j, ch in enumerate(text[len(head):]):
        k = (j + 1) / max(1, min(tail, n))
        out.append(_gray(level + (peak - level) * k) + ch)
    out.append(RESET)
    return "".join(out)


# ── 度量：显示宽度 ────────────────────────────────────

def _cell_width(ch):
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def display_width(s):
    """终端显示列数（CJK 宽字符按 2 列；忽略 ANSI 转义）。"""
    w, i, n = 0, 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\033":  # 跳过 CSI 序列
            j = i + 1
            if j < n and s[j] == "[":
                j += 1
                while j < n and not (s[j].isalpha() or s[j] == "~"):
                    j += 1
                i = j + 1
            else:
                i = j
            continue
        if ch == "\ufe0f":
            # 变体选择符 16：强制 emoji 呈现，终端按 2 列画（基字符本身多按 1 列算）
            if i > 0 and _cell_width(s[i - 1]) == 1:
                w += 1
            i += 1
            continue
        if ch in ("\u200d", "\u200b"):  # 零宽连接符/零宽空格不占列
            i += 1
            continue
        w += _cell_width(ch)
        i += 1
    return w


def wrap_width(text, width):
    """按显示列数换行（CJK 可在任意字符处断开；连续 ASCII 词尽量整词换行）。"""
    width = max(1, width)
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        cur, cur_w, last_space = "", 0, -1
        for ch in paragraph:
            cw = _cell_width(ch)
            if cur_w + cw > width and cur:
                if ch != " " and last_space > 0 and ch.isascii() and cur[-1].isascii() and cur[-1] != " ":
                    # ASCII 词中间：回退到最近的空格整词换行
                    lines.append(cur[:last_space].rstrip())
                    cur = cur[last_space + 1:]
                    cur_w = display_width(cur)
                else:
                    lines.append(cur.rstrip())
                    cur, cur_w = "", 0
                last_space = -1
                if ch == " " and not cur:
                    continue
            if ch == " ":
                last_space = len(cur)
            cur += ch
            cur_w += cw
        lines.append(cur.rstrip())
    return lines


def pad_to(s, width):
    """右侧补空格到 width 列（按显示宽度）。"""
    return s + " " * max(0, width - display_width(s))


def clip_to(s, width):
    """按显示宽度截断（不处理 ANSI，供纯文本使用）。"""
    out, w = [], 0
    for ch in s:
        cw = _cell_width(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out)


# ── 时间驱动的小动画 ──────────────────────────────────

BRAILLE_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spinner(now, period=0.8):
    """盲文旋转指示器（活着的证据，占 1 列）。"""
    if not ENABLED:
        return "·"
    idx = int((now % period) / period * len(BRAILLE_SPINNER))
    return paint(BRAILLE_SPINNER[idx], MUTED)


def pulse(now, period=1.2):
    """0~1 正弦脉动。"""
    return 0.5 - 0.5 * math.cos(2 * math.pi * (now % period) / period)


def rec_dot(now):
    """录音指示点：明暗呼吸（不用红色，用亮度）。"""
    if not ENABLED:
        return "●"
    return paint("●", VOID + (HI - VOID) * pulse(now, 1.6), bold=True)


def cursor(now, period=1.0):
    """闪烁光标块。"""
    if not ENABLED:
        return "▌"
    return paint("▌", HI, bold=True) if (now % period) < period * 0.6 else " "


# ── 猫 ───────────────────────────────────────────────
#
# 参考 oneko（X11 上那只追鼠标的猫）的做法：不是一个静态图标，而是一只有姿态、
# 有行为的小动物——会走、走到墙边会挠两下再转身、停下来会坐着、坐久了伸懒腰、
# 一直没动静就趴下睡（冒 z），有动静立刻竖耳朵。
# 精灵是 3 行 × 7 列的定宽字符画，尾巴挂在身后一列、状态标记（z / !）挂在头顶一列。

# 眨眼节律：猫的自发眨眼稀疏而慢，取 ~4 秒一次、闭眼 0.18 秒。
BLINK_PERIOD = 4.0
BLINK_CLOSED = 0.18

CAT_W = 7          # 身体宽度（列）
CAT_H = 3          # 身体高度（行）

_POSES = {
    "sit":     (" /\\_/\\ ", "( o.o )", " > ^ < "),   # 坐着
    "blink":   (" /\\_/\\ ", "( -.- )", " > ^ < "),   # 眨眼
    "alert":   (" /\\_/\\ ", "( O.O )", " > ^ < "),   # 竖耳朵：听见动静
    "walk1":   (" /\\_/\\ ", "( o.o )", " ^^ ^^ "),   # 走：左右脚
    "walk2":   (" /\\_/\\ ", "( o.o )", " ^  ^^ "),
    "stretch": (" /\\_/\\ ", "( ~.~ )", "<  ^  >"),   # 伸懒腰
    "scratch": (" /\\_/\\ ", "( >.< )", " ///// "),   # 挠墙
    "sleep":   (" /\\_/\\ ", "( -.- )", " >_^_< "),   # 趴着睡
}
_TAIL_SWING = "~-,-"


def _blinking(now, phase=0.0):
    return ((now + phase) % BLINK_PERIOD) < BLINK_CLOSED


class Cat:
    """一只有行为的小猫：走 → 坐 → 伸懒腰 → 睡；撞墙先挠两下再转身。

    poke() 表示"有动静"（有人说话、出了一句字），猫会立刻竖耳朵并醒过来；
    rest() 表示"没动静"，安静够久它就睡了。roam=False 时只在原地做动作。
    """

    SPEED = 6.0            # 走路速度（列/秒）
    SLEEP_AFTER = 20.0     # 安静这么久就睡
    ALERT_SECONDS = 0.8

    def __init__(self, width, height=CAT_H, seed=None, roam=True):
        self._rng = random.Random(seed)
        self.roam = roam
        self._place(width, height, center=not roam)
        self.dir = 1
        self.state = "sit"
        self._until = 0.0
        self._calm_since = None
        self._last = None

    def _place(self, width, height, center=False):
        self.w = max(CAT_W, int(width))
        self.h = max(CAT_H, int(height))
        span = max(0, self.w - CAT_W)
        self.x = span / 2.0 if center else self._rng.uniform(0, span)
        self.row = 0 if self.h <= CAT_H else self._rng.randrange(self.h - CAT_H + 1)

    def resize(self, width, height=CAT_H):
        if (int(width), int(height)) != (self.w, self.h):
            self._place(width, height, center=not self.roam)

    # ── 外界的动静 ────────────────────────────────────
    def poke(self, now):
        """有动静：竖耳朵，并把"安静了多久"清零。"""
        self._calm_since = None
        if self.state != "alert":
            self.state = "alert"
            self._until = now + self.ALERT_SECONDS

    def rest(self, now):
        if self._calm_since is None:
            self._calm_since = now

    # ── 行为 ─────────────────────────────────────────
    def update(self, now):
        if self._last is None:
            self._last, self._until = now, now + 1.0
        dt = max(0.0, min(0.25, now - self._last))
        self._last = now
        if self.state == "walk":
            self.x += self.dir * self.SPEED * dt
            span = max(0, self.w - CAT_W)
            if self.x <= 0 or self.x >= span:
                self.x = min(max(self.x, 0.0), float(span))
                self.dir = -self.dir
                self.state, self._until = "scratch", now + 1.2   # 撞墙：挠两下再转身
                if self.h > CAT_H:
                    self.row = self._rng.randrange(self.h - CAT_H + 1)
        if now < self._until:
            return
        calm = self._calm_since is not None and (now - self._calm_since) > self.SLEEP_AFTER
        self.state, self._until = self._next_state(now, calm)

    def _next_state(self, now, calm):
        r = self._rng.random()
        if calm:
            return "sleep", now + 5.0
        if self.state == "sleep":                       # 刚被吵醒：先伸个懒腰
            return "stretch", now + 1.6
        if self.state == "walk":
            return "sit", now + self._rng.uniform(0.8, 2.0)
        if self.roam and r < 0.7:
            return "walk", now + self._rng.uniform(1.5, 4.0)
        if r > 0.9:
            return "stretch", now + 1.4
        return "sit", now + self._rng.uniform(1.0, 2.5)

    # ── 画 ───────────────────────────────────────────
    def pose(self, now):
        if self.state == "walk":
            return _POSES["walk1" if int(now * 6) % 2 == 0 else "walk2"]
        if self.state == "sit" and _blinking(now):
            return _POSES["blink"]
        return _POSES.get(self.state, _POSES["sit"])

    def tail(self, now):
        if self.state == "sleep":
            return "_"
        return _TAIL_SWING[int(now * 3) % len(_TAIL_SWING)]

    def marker(self, now):
        if self.state == "sleep":
            return "z" if int(now * 1.5) % 2 == 0 else "Z"
        return "!" if self.state == "alert" else ""

    def cells(self, now, opaque=False):
        """[(行, 列, 字符)]：叠到别的画面上用。

        opaque=True 时连身体内部的空格一起给出——猫是实心的，背后的东西
        （比如数字雨）不该从它肚子里漏出来。
        """
        x0 = int(round(self.x))
        out = []
        for r, line in enumerate(self.pose(now)):
            for c, ch in enumerate(line):
                if ch != " " or opaque:
                    out.append((self.row + r, x0 + c, ch))
        behind = x0 - 1 if self.dir > 0 else x0 + CAT_W
        out.append((self.row + 1, behind, self.tail(now)))
        mark = self.marker(now)
        if mark:
            out.append((self.row, x0 + CAT_W if self.dir > 0 else x0 - 1, mark))
        return out

    def render(self, width, now):
        """在一条 width 宽、CAT_H 行高的跑道上画出这只猫（返回纯文本行）。"""
        width = max(1, int(width))
        grid = [[" "] * width for _ in range(CAT_H)]
        for r, c, ch in self.cells(now):
            if 0 <= r < CAT_H and 0 <= c < width:
                grid[r][c] = ch
        return ["".join(row) for row in grid]


# ── 表头弹道与峰值保持 ────────────────────────────────

class Ballistics:
    """VU 表弹道：一阶惯性，300 ms 达到 99%（IEC 60268-17）。

    让电平条像真表针一样有惯性，而不是逐帧抖动——动感来自物理，不是随机数。
    """

    RISE_SECONDS = 0.300  # 到 99% 所需时间
    TAU = RISE_SECONDS / math.log(100.0)

    def __init__(self):
        self.value = 0.0
        self._last = None

    def update(self, target, now):
        if self._last is None:
            self._last = now
            self.value = target
            return self.value
        dt = max(0.0, now - self._last)
        self._last = now
        a = 1.0 - math.exp(-dt / self.TAU) if dt > 0 else 0.0
        self.value += (target - self.value) * a
        return self.value


class PeakHold:
    """峰值保持：保持 hold 秒后按 IEC 60268-18（PPM）回落速率下降——
    20 dB / 1.7 s；换算到本表 60 dB 量程即每秒 (20/1.7)/60 的满刻度比例。
    """

    HOLD_SECONDS = 1.0
    FALL_PER_SECOND = (20.0 / 1.7) / -METER_FLOOR_DBFS

    def __init__(self):
        self.peak = 0.0
        self._since = 0.0
        self._last = None

    def update(self, level, now):
        if self._last is None:
            self._last = now
        dt = max(0.0, now - self._last)
        self._last = now
        if level >= self.peak:
            self.peak, self._since = level, now
        elif now - self._since > self.HOLD_SECONDS:
            self.peak = max(level, self.peak - self.FALL_PER_SECOND * dt)
        return self.peak


# ── 电平表 ───────────────────────────────────────────

# 亚格填充（1/8 列分辨率）：让表头连续移动而不是一格一格跳
_PARTIAL = " ▏▎▍▌▋▊▉█"
_TRACK = "─"
_TICK = "┼"

_ALIGN_POS = (ALIGN_DBFS - METER_FLOOR_DBFS) / -METER_FLOOR_DBFS
_HEADROOM_POS = (HEADROOM_DBFS - METER_FLOOR_DBFS) / -METER_FLOOR_DBFS


def meter_bar(level, width, peak=None):
    """单色电平条：亚格填充 + 明度自左向右升高 + 峰值标记 + 绝对刻度线。

    刻度线画在 -20 dBFS（对齐电平）和 -6 dBFS（留空电平）处，未被填满时可见，
    因此条子的读数有绝对意义。超过 -6 dBFS 的部分加粗提示逼近满刻度。
    """
    width = max(1, width)
    level = min(1.0, max(0.0, level))
    exact = level * width
    full = int(exact)
    frac = exact - full
    peak_i = None
    if peak is not None and peak > 0:
        peak_i = min(width - 1, int(round(peak * width)) - 1)
    ticks = {min(width - 1, int(_ALIGN_POS * width)),
             min(width - 1, int(_HEADROOM_POS * width))}

    if not ENABLED:
        cells = []
        for i in range(width):
            if i < full:
                cells.append("█")
            elif i == full and frac >= 0.5:
                cells.append("▌")
            elif peak_i is not None and i == peak_i:
                cells.append("▏")
            elif i in ticks:
                cells.append(_TICK)
            else:
                cells.append(_TRACK)
        return "".join(cells)

    out = []
    for i in range(width):
        t = i / max(1, width - 1)
        lit = INK + (HI - INK) * t          # 越靠右越亮：像被推到刻度尽头
        hot = t >= _HEADROOM_POS
        if i < full:
            out.append(_gray(lit) + (BOLD if hot else "") + "█" + (RESET if hot else ""))
        elif i == full and frac > 0.06:
            out.append(_gray(lit) + _PARTIAL[int(frac * 8)])
        elif peak_i is not None and i == peak_i:
            out.append(_gray(HI) + BOLD + "▏" + RESET)
        elif i in ticks:
            out.append(_gray(FAINT) + _TICK)
        else:
            out.append(_gray(VOID) + _TRACK)
    return "".join(out) + RESET


# ── 声音时间轴（“磁带”）──────────────────────────────

_BLOCKS = " ▁▂▃▄▅▆▇█"


def render_tape(levels, width, covered_cells=0, rows=1, marks=()):
    """录音时间轴：每格一个时间片，柱高 = 该片的电平（同一 dBFS 绝对刻度）。

    功能而非装饰：
      · 柱高看得出音量起伏，谷底就是停顿——一眼知道断句会落在哪；
      · 已被识别定稿覆盖的部分用正常墨色，尚未定稿的尾巴压暗，
        两者的分界就是"转写落后了多少"；
      · 最新一格提亮，磁带向左走，走多快就是说了多久。

    Args:
        levels: 从旧到新的电平序列（0~1）。
        covered_cells: 已定稿覆盖到第几格（绝对下标）。
        marks: 需要标注的绝对下标集合（如断句点），在基线上打点。
    返回自上而下的行列表。
    """
    width, rows = max(1, width), max(1, rows)
    levels = list(levels)
    start = max(0, len(levels) - width)
    view = levels[start:]
    total_steps = rows * 8
    steps = [min(total_steps, max(0, int(round(v * total_steps)))) for v in view]
    last = len(view) - 1
    lines = []
    for r in range(rows):
        base = (rows - 1 - r) * 8
        cells = []
        for i in range(width):
            if i >= len(view):
                cells.append(paint(" ", VOID) if ENABLED else " ")
                continue
            s = min(8, max(0, steps[i] - base))
            absolute = start + i
            ch = _BLOCKS[s] if s > 0 else (" " if r < rows - 1 else "▁")
            if absolute in marks and r == rows - 1:
                # 断句落点画在基线上：这是信息，不是装饰，关特效也要看得见
                cells.append(paint("╹", HI, bold=True) if ENABLED else "╹")
                continue
            if not ENABLED:
                cells.append(ch)
                continue
            if i == last:
                lvl, bold = HI, True                      # 磁带头：最新的一格
            elif absolute < covered_cells:
                lvl, bold = INK, False                    # 已定稿覆盖
            else:
                lvl, bold = VOID, False                   # 还没定稿的尾巴
            if s == 0 and r == rows - 1:
                lvl = min(lvl, FAINT)                     # 基线不抢戏
            cells.append(_gray(lvl) + (BOLD if bold else "") + ch + (RESET if bold else ""))
        lines.append("".join(cells) + (RESET if ENABLED else ""))
    return lines


# ── 打字机 ───────────────────────────────────────────

class Typewriter:
    """打字机揭示：文本逐字出现；文本变短（临时结果被修正）时立即对齐。

    cps 是显示节奏（字符/秒）；为避免落后于流式结果太多，揭示速度至少保证
    1 秒内追平当前全文。
    """

    def __init__(self, cps=40.0):
        self.cps = cps
        self.shown = 0
        self._last = None

    def visible(self, text, now):
        if not ENABLED:
            return text
        if self._last is None:
            self._last = now
        dt = max(0.0, now - self._last)
        self._last = now
        remaining = len(text) - self.shown
        if remaining <= 0:
            self.shown = len(text)
            return text
        rate = max(self.cps, remaining / 1.0)
        self.shown = min(len(text), self.shown + int(math.ceil(rate * dt)))
        return text[: self.shown]


# ── 猫之矩阵（空闲屏保）─────────────────────────────

_RAIN_CHARS = (
    "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"
    "0123456789<>=+-*/|:.;"
)

RAIN_HEAD = 252
RAIN_BODY = 176
RAIN_TAIL = 40

class _Eyes:
    """雨幕深处偶尔亮起、眨一下又灭掉的一对眼睛。"""

    LIFE = 3.2

    def __init__(self, width, height, rng):
        self.rng, self.w, self.h = rng, width, height
        self.born = -1e9
        self.pos = (0, 0)

    def resize(self, width, height):
        self.w, self.h = width, height

    def step(self, now):
        age = now - self.born
        if age > self.LIFE + self.rng.uniform(2.0, 8.0):
            self.born = now
            self.pos = (self.rng.randrange(max(1, self.h - 1)),
                        self.rng.randrange(max(1, self.w - 3)))

    def draw(self, grid_rows, now):
        age = now - self.born
        if not (0 <= age <= self.LIFE):
            return
        r, c = self.pos
        if not (0 <= r < len(grid_rows)):
            return
        # 淡入 → 稳定 → 眨一下 → 淡出
        glow = math.sin(math.pi * min(1.0, age / self.LIFE)) ** 0.6
        shut = 1.55 < age < 1.72
        pair = ("-", "-") if shut else ("●", "●")
        for dc, ch in ((0, pair[0]), (2, pair[1])):
            if 0 <= c + dc < len(grid_rows[r]):
                grid_rows[r][c + dc] = ("eye", ch, glow)


class MatrixRain:
    """猫之矩阵：单色数字雨 + 在雨里散步的猫 + 暗处亮起的眼睛。

    render() 返回 rows 行字符串。
    """

    def __init__(self, width, height, seed=None, cats=True):
        self.w, self.h = max(1, width), max(1, height)
        self._rng = random.Random(seed)
        self._grid = [[" "] * self.w for _ in range(self.h)]
        self._age = [[10 ** 6] * self.w for _ in range(self.h)]  # 每格自被雨头经过后的帧数
        # 每列：雨头所在行（可为负=尚未进入）、速度（每帧行数）、拖尾长度
        self._heads = [self._rng.uniform(-self.h, 0) for _ in range(self.w)]
        self._speed = [self._rng.uniform(0.3, 1.0) for _ in range(self.w)]
        self._tail = [self._rng.randint(4, max(5, self.h // 2)) for _ in range(self.w)]
        self.cats = cats
        self._cat = Cat(self.w, self.h, seed=self._rng.random(), roam=True) if cats else None
        self._eyes = _Eyes(self.w, self.h, self._rng) if cats else None
        self._last_t = None

    def resize(self, width, height):
        if (width, height) != (self.w, self.h):
            self.__init__(width, height, cats=self.cats)

    def step(self, now=None):
        for r in range(self.h):
            row = self._age[r]
            for c in range(self.w):
                row[c] += 1
        for c in range(self.w):
            self._heads[c] += self._speed[c]
            head = int(self._heads[c])
            if 0 <= head < self.h:
                self._grid[head][c] = self._rng.choice(_RAIN_CHARS)
                self._age[head][c] = 0
            if head - self._tail[c] > self.h:
                self._heads[c] = self._rng.uniform(-self.h, -1)
                self._speed[c] = self._rng.uniform(0.3, 1.0)
                self._tail[c] = self._rng.randint(4, max(5, self.h // 2))
        # 偶尔让拖尾里的字符“闪变”
        for _ in range(max(1, self.w * self.h // 40)):
            r, c = self._rng.randrange(self.h), self._rng.randrange(self.w)
            if self._age[r][c] < self._tail[c]:
                self._grid[r][c] = self._rng.choice(_RAIN_CHARS)
        if self._cat is not None:
            t = time.monotonic() if now is None else now
            self._last_t = t
            self._cat.update(t)
            self._eyes.step(t)

    def render(self, now=None):
        t = time.monotonic() if now is None else now
        # 先铺一层雨（("rain", ch, age, tail)），再让猫和眼睛盖上去
        cells = []
        for r in range(self.h):
            row = []
            for c in range(self.w):
                age, tail = self._age[r][c], self._tail[c]
                row.append(None if age >= tail else ("rain", self._grid[r][c], age, tail))
            cells.append(row)
        if self._cat is not None:
            self._eyes.draw(cells, t)                          # 眼睛先画，猫盖在上面
            for r, c, ch in self._cat.cells(t, opaque=True):   # 猫是实心的：雨不从肚子里漏
                if 0 <= r < len(cells) and 0 <= c < len(cells[r]):
                    cells[r][c] = ("cat", ch)

        lines = []
        for row in cells:
            out = []
            for cell in row:
                if cell is None:
                    out.append(" ")
                    continue
                kind = cell[0]
                if kind == "cat":
                    out.append(paint(cell[1], HI, bold=True) if ENABLED else cell[1])
                elif kind == "eye":
                    out.append(paint(cell[1], VOID + (HI - VOID) * cell[2], bold=True)
                               if ENABLED else cell[1])
                else:
                    _, ch, age, tail = cell
                    if not ENABLED:
                        out.append(ch)
                    elif age == 0:
                        out.append(_gray(RAIN_HEAD) + BOLD + ch + RESET)
                    else:
                        k = age / tail
                        out.append(_gray(RAIN_BODY + (RAIN_TAIL - RAIN_BODY) * k) + ch)
            lines.append("".join(out) + (RESET if ENABLED else ""))
        return lines


# ── 标题与细线 ───────────────────────────────────────

def banner(width, now=0.0):
    """标题：字距拉开的字标 + 一束缓慢掠过的高光。"""
    title = "SyncListen"
    if width >= 22:
        title = " ".join(title)          # 字距：安静的高级感
    if display_width(title) > width:
        title = "SyncListen"[: max(1, width)]
    return sweep(title, now, speed=0.18, base=MUTED, peak=HI, bold=True, sigma=0.10) if ENABLED else title


def rule(width, now=None, active=False):
    """细线：两端淡出、中段稍亮；active 时有一束高光沿线游走。"""
    width = max(1, width)
    line = "─" * width
    if not ENABLED:
        return line
    if active and now is not None:
        return sweep(line, now, speed=0.22, base=VOID, peak=INK, sigma=0.07)
    out = []
    for i in range(width):
        # 两端淡出：正弦包络，中间最亮
        k = math.sin(math.pi * (i / max(1, width - 1))) ** 0.5
        out.append(_gray(VOID + (FAINT - VOID) * k) + "─")
    return "".join(out) + RESET


def elapsed_text(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def now():
    return time.monotonic()
