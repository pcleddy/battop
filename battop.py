#!/usr/bin/env python3
"""
battop - a minimal battery history chart for macOS

History is saved to ~/.battop_history.json and persists across sessions.

Usage: python3 battop.py
Keys:  q = quit, r = force refresh, c = clear history
"""

import curses
import subprocess
import re
import time
import sys
import signal
import json
from pathlib import Path

# ── History ─────────────────────────────────────────────────────────────────

HISTORY_FILE = Path.home() / ".battop_history.json"
MAX_HISTORY_AGE_HOURS = 24
SAMPLE_INTERVAL_SEC = 60


def load_history():
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
            cutoff = time.time() - MAX_HISTORY_AGE_HOURS * 3600
            return [e for e in data if e.get("t", 0) >= cutoff]
        except (json.JSONDecodeError, KeyError):
            return []
    return []


def save_history(history):
    try:
        cutoff = time.time() - MAX_HISTORY_AGE_HOURS * 3600
        history = [e for e in history if e.get("t", 0) >= cutoff]
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f)
    except OSError:
        pass


def record_sample(history, stats):
    now = time.time()
    if history and (now - history[-1]["t"]) < SAMPLE_INTERVAL_SEC:
        return False
    history.append({
        "t": now,
        "pct": stats.get("percent", -1),
        "chg": stats.get("charging", False),
    })
    return True


# ── Data Collection ─────────────────────────────────────────────────────────

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return ""


def compute_stats():
    # pmset
    raw = run_cmd("pmset -g batt")
    percent = -1
    match = re.search(r"(\d+)%", raw)
    if match:
        percent = int(match.group(1))

    # ioreg
    ioreg_raw = run_cmd(
        'ioreg -rc AppleSmartBattery | grep -E "(IsCharging|ExternalConnected)"'
    )
    charging = False
    for line in ioreg_raw.splitlines():
        if "IsCharging" in line and "Yes" in line:
            charging = True

    return {"percent": percent, "charging": charging}


# ── Drawing ─────────────────────────────────────────────────────────────────

C_TITLE = 1
C_HEADER = 2
C_GOOD = 3
C_WARN = 4
C_CRIT = 5
C_DIM = 6
C_ACCENT = 7
C_CHART_BAT = 8
C_CHART_CHG = 9

BLOCKS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_WHITE, -1)
    curses.init_pair(C_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(C_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CRIT, curses.COLOR_RED, -1)
    curses.init_pair(C_DIM, 8, -1)
    curses.init_pair(C_ACCENT, curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_CHART_BAT, curses.COLOR_CYAN, -1)
    curses.init_pair(C_CHART_CHG, curses.COLOR_GREEN, -1)


def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        win.addnstr(y, x, text, max_len, attr)
    except curses.error:
        pass


def pct_color(val):
    if val <= 10:
        return curses.color_pair(C_CRIT)
    elif val <= 25:
        return curses.color_pair(C_WARN)
    else:
        return curses.color_pair(C_CHART_BAT)


def draw(stdscr, stats, last_update, history):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    dim = curses.color_pair(C_DIM)
    accent = curses.color_pair(C_ACCENT) | curses.A_BOLD

    if h < 10 or w < 40:
        safe_addstr(stdscr, 0, 0, "Terminal too small", curses.color_pair(C_CRIT))
        stdscr.refresh()
        return

    # ── Title ───────────────────────────────────────────────
    pct = stats["percent"] if stats["percent"] >= 0 else 0
    charging = stats["charging"]

    if charging:
        status = f"⚡ {pct}% Charging"
    else:
        status = f"🔋 {pct}%"

    title = f" battop  ·  {status} "
    pad = "─" * max(0, (w - len(title) - 2) // 2)
    title_line = pad + title + pad
    if len(title_line) < w - 1:
        title_line += "─" * (w - 1 - len(title_line))
    safe_addstr(stdscr, 0, 0, title_line, accent)

    # ── Chart area ──────────────────────────────────────────
    y_axis_w = 5
    margin_x = 2
    plot_x = margin_x + y_axis_w
    plot_w = w - plot_x - 2
    chart_top = 2
    chart_bot_row = h - 5   # leave room for axis + time labels + legend + footer
    chart_h = chart_bot_row - chart_top + 1

    if chart_h < 4 or plot_w < 10:
        safe_addstr(stdscr, 2, 2, "Window too small for chart", dim)
        stdscr.refresh()
        return

    # ── Y-axis + grid ───────────────────────────────────────
    for row_idx in range(chart_h):
        pct_val = 100 - (100 * row_idx / (chart_h - 1)) if chart_h > 1 else 100
        screen_y = chart_top + row_idx

        # Labels at 0%, 25%, 50%, 75%, 100%
        step = max(1, (chart_h - 1) // 4)
        if row_idx % step == 0 or row_idx == chart_h - 1:
            label = f"{int(round(pct_val)):>3}%"
            safe_addstr(stdscr, screen_y, margin_x, label, dim)

        safe_addstr(stdscr, screen_y, plot_x - 1, "│", dim)

        # Grid dots
        for cx in range(plot_w):
            if cx % 10 == 0:
                safe_addstr(stdscr, screen_y, plot_x + cx, "·", dim)

    # Bottom axis
    axis_y = chart_bot_row + 1
    safe_addstr(stdscr, axis_y, plot_x - 1, "└" + "─" * plot_w, dim)

    # ── Plot data ───────────────────────────────────────────
    if history and len(history) >= 2:
        t_min = history[0]["t"]
        t_max = history[-1]["t"]
        t_span = max(t_max - t_min, 1)

        # Bucket samples into columns
        bucket_pct = [None] * plot_w
        bucket_chg = [False] * plot_w

        for entry in history:
            frac = (entry["t"] - t_min) / t_span
            col = int(frac * (plot_w - 1))
            col = max(0, min(col, plot_w - 1))
            if bucket_pct[col] is None:
                bucket_pct[col] = []
            bucket_pct[col].append(entry["pct"])
            if entry.get("chg"):
                bucket_chg[col] = True

        # Average buckets
        values = [None] * plot_w
        for i in range(plot_w):
            if bucket_pct[i] is not None:
                values[i] = sum(bucket_pct[i]) / len(bucket_pct[i])

        # Interpolate gaps
        last_val = None
        for i in range(plot_w):
            if values[i] is not None:
                last_val = values[i]
            elif last_val is not None:
                values[i] = last_val
        last_val = None
        for i in range(plot_w - 1, -1, -1):
            if values[i] is not None:
                last_val = values[i]
            elif last_val is not None:
                values[i] = last_val

        # Draw columns
        total_sub = chart_h * 8
        for cx in range(plot_w):
            val = values[cx]
            if val is None:
                continue

            fill_sub = int(val / 100.0 * total_sub)
            fill_sub = max(0, min(fill_sub, total_sub))
            full_rows = fill_sub // 8
            remainder = fill_sub % 8

            if bucket_chg[cx]:
                col_color = curses.color_pair(C_CHART_CHG) | curses.A_BOLD
            else:
                col_color = pct_color(val)

            for row_from_bottom in range(chart_h):
                screen_y = chart_bot_row - row_from_bottom
                if row_from_bottom < full_rows:
                    safe_addstr(stdscr, screen_y, plot_x + cx, "█", col_color)
                elif row_from_bottom == full_rows and remainder > 0:
                    safe_addstr(stdscr, screen_y, plot_x + cx, BLOCKS[remainder], col_color)

        # Time labels
        label_y = axis_y + 1
        for frac, align in [(0.0, "left"), (0.5, "center"), (1.0, "right")]:
            t_val = t_min + t_span * frac
            label = time.strftime("%H:%M", time.localtime(t_val))
            cx = int(frac * (plot_w - 1))
            if align == "center":
                cx = max(0, cx - len(label) // 2)
            elif align == "right":
                cx = max(0, cx - len(label) + 1)
            safe_addstr(stdscr, label_y, plot_x + cx, label, dim)

        # Legend + stats
        legend_y = label_y + 1
        safe_addstr(stdscr, legend_y, plot_x, "█", curses.color_pair(C_CHART_BAT))
        safe_addstr(stdscr, legend_y, plot_x + 1, " Battery  ", dim)
        safe_addstr(stdscr, legend_y, plot_x + 11, "█", curses.color_pair(C_CHART_CHG) | curses.A_BOLD)
        safe_addstr(stdscr, legend_y, plot_x + 12, " Charging", dim)

        pcts = [e["pct"] for e in history if e.get("pct", -1) >= 0]
        if pcts:
            hi, lo = max(pcts), min(pcts)
            delta = pcts[-1] - pcts[0]
            sign = "+" if delta >= 0 else ""
            summary = f"  High:{hi}%  Low:{lo}%  Net:{sign}{delta}%"
            safe_addstr(stdscr, legend_y, plot_x + 22, summary, curses.color_pair(C_HEADER))
    else:
        msg = "Collecting data... chart appears after a few minutes."
        safe_addstr(stdscr, chart_top + chart_h // 2, plot_x + 2, msg, dim)

    # ── Footer ──────────────────────────────────────────────
    footer_y = h - 1
    ts = time.strftime("%H:%M:%S", time.localtime(last_update))
    n = len(history)
    footer = f" {ts} · {n} samples · [r]efresh [c]lear [q]uit"
    safe_addstr(stdscr, footer_y, margin_x, footer, dim)

    stdscr.refresh()


# ── Main ────────────────────────────────────────────────────────────────────

def main(stdscr):
    curses.curs_set(0)
    stdscr.timeout(300)
    init_colors()

    refresh_interval = 3
    last_refresh = 0
    stats = {}
    history = load_history()
    last_save = time.time()

    while True:
        now = time.time()

        if now - last_refresh >= refresh_interval:
            stats = compute_stats()
            last_refresh = now

            if record_sample(history, stats):
                if now - last_save > 300 or len(history) < 5:
                    save_history(history)
                    last_save = now

        if stats:
            draw(stdscr, stats, last_refresh, history)

        key = stdscr.getch()
        if key == ord("q") or key == ord("Q"):
            save_history(history)
            break
        elif key == ord("r") or key == ord("R"):
            last_refresh = 0
        elif key == ord("c") or key == ord("C"):
            history.clear()
            save_history(history)
        elif key == curses.KEY_RESIZE:
            stdscr.clear()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    import platform
    if platform.system() != "Darwin":
        print("⚠️  battop is designed for macOS.")
        print("   Running anyway for demo purposes...\n")

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        save_history(load_history())
