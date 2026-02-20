#!/usr/bin/env python3
"""
netop - a minimal network traffic chart for macOS

Shows a mirrored chart: received (up/cyan) and sent (down/red).
History is saved to ~/.netop_history.json and persists across sessions.

Usage: python3 netop.py
Keys:  q = quit, r = force refresh, c = clear history, i = cycle interface
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

HISTORY_FILE = Path.home() / ".netop_history.json"
MAX_HISTORY_AGE_HOURS = 1  # keep last hour (network is chattier than battery)
SAMPLE_INTERVAL_SEC = 2


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
        "rx": stats.get("rx_bps", 0),
        "tx": stats.get("tx_bps", 0),
    })
    return True


# ── Data Collection ─────────────────────────────────────────────────────────

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return ""


def get_interfaces():
    """Get list of active network interfaces."""
    raw = run_cmd("netstat -ib")
    interfaces = []
    seen = set()
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 7:
            iface = parts[0]
            # Skip loopback and duplicates
            if iface.startswith("lo") or iface in seen:
                continue
            # Only include interfaces with traffic
            try:
                ibytes = int(parts[6])
                if ibytes > 0:
                    interfaces.append(iface)
                    seen.add(iface)
            except (ValueError, IndexError):
                pass
    return interfaces if interfaces else ["en0"]


def get_interface_bytes(interface="en0"):
    """Get cumulative bytes in/out for an interface."""
    raw = run_cmd("netstat -ib")
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 10 and parts[0] == interface:
            try:
                # netstat -ib columns: Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes
                ibytes = int(parts[6])
                obytes = int(parts[9])
                return ibytes, obytes
            except (ValueError, IndexError):
                continue
    return 0, 0


class RateTracker:
    """Track byte rates across samples."""

    def __init__(self):
        self.prev_rx = None
        self.prev_tx = None
        self.prev_time = None
        self.total_rx_session = 0
        self.total_tx_session = 0

    def update(self, rx_bytes, tx_bytes):
        now = time.time()
        stats = {
            "rx_bps": 0,
            "tx_bps": 0,
            "rx_total": rx_bytes,
            "tx_total": tx_bytes,
            "rx_session": self.total_rx_session,
            "tx_session": self.total_tx_session,
        }

        if self.prev_rx is not None and self.prev_time is not None:
            dt = now - self.prev_time
            if dt > 0:
                drx = max(0, rx_bytes - self.prev_rx)
                dtx = max(0, tx_bytes - self.prev_tx)
                stats["rx_bps"] = drx / dt
                stats["tx_bps"] = dtx / dt
                self.total_rx_session += drx
                self.total_tx_session += dtx
                stats["rx_session"] = self.total_rx_session
                stats["tx_session"] = self.total_tx_session

        self.prev_rx = rx_bytes
        self.prev_tx = tx_bytes
        self.prev_time = now
        return stats

    def reset(self):
        self.prev_rx = None
        self.prev_tx = None
        self.prev_time = None


# ── Drawing ─────────────────────────────────────────────────────────────────

C_TITLE = 1
C_HEADER = 2
C_RX = 3      # received - cyan
C_TX = 4      # sent - red
C_DIM = 5
C_ACCENT = 6
C_GOOD = 7
C_WARN = 8

BLOCKS_UP = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
BLOCKS_DN = [" ", "▔", "▔", "▔", "▀", "▀", "▀", "▀", "█"]
# For the downward bars we'll use inverse blocks drawn from top


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_WHITE, -1)
    curses.init_pair(C_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(C_RX, curses.COLOR_CYAN, -1)
    curses.init_pair(C_TX, curses.COLOR_RED, -1)
    curses.init_pair(C_DIM, 8, -1)
    curses.init_pair(C_ACCENT, curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)


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


def format_bytes(b):
    """Format byte count to human readable."""
    if b < 1024:
        return f"{b:.0f} B"
    elif b < 1024 ** 2:
        return f"{b/1024:.1f} KB"
    elif b < 1024 ** 3:
        return f"{b/1024**2:.1f} MB"
    else:
        return f"{b/1024**3:.2f} GB"


def format_rate(bps):
    """Format bytes/sec to human readable rate."""
    if bps < 1024:
        return f"{bps:.0f} B/s"
    elif bps < 1024 ** 2:
        return f"{bps/1024:.1f} KB/s"
    elif bps < 1024 ** 3:
        return f"{bps/1024**2:.1f} MB/s"
    else:
        return f"{bps/1024**3:.2f} GB/s"


def draw(stdscr, stats, last_update, history, interface, interfaces):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    dim = curses.color_pair(C_DIM)
    accent = curses.color_pair(C_ACCENT) | curses.A_BOLD
    rx_color = curses.color_pair(C_RX) | curses.A_BOLD
    tx_color = curses.color_pair(C_TX) | curses.A_BOLD

    if h < 12 or w < 50:
        safe_addstr(stdscr, 0, 0, "Terminal too small", curses.color_pair(C_TX))
        stdscr.refresh()
        return

    # ── Title ───────────────────────────────────────────────
    rx_rate = format_rate(stats.get("rx_bps", 0))
    tx_rate = format_rate(stats.get("tx_bps", 0))
    title = f" netop  ·  {interface}  ·  ▼ {rx_rate}  ▲ {tx_rate} "
    pad = "─" * max(0, (w - len(title) - 2) // 2)
    title_line = pad + title + pad
    if len(title_line) < w - 1:
        title_line += "─" * (w - 1 - len(title_line))
    safe_addstr(stdscr, 0, 0, title_line, accent)

    # ── Chart layout ────────────────────────────────────────
    # Mirrored chart: top half = RX (up), bottom half = TX (down)
    # with a center axis line between them
    y_axis_w = 9  # wider for rate labels
    margin_x = 1
    plot_x = margin_x + y_axis_w
    plot_w = w - plot_x - 2
    stats_rows = 3  # bottom stats area
    chart_top = 2
    chart_bot = h - stats_rows - 2

    total_chart_rows = chart_bot - chart_top + 1
    if total_chart_rows < 6 or plot_w < 10:
        safe_addstr(stdscr, 2, 2, "Window too small for chart", dim)
        stdscr.refresh()
        return

    # Split into upper (RX) and lower (TX) halves
    mid_y = chart_top + total_chart_rows // 2
    rx_rows = mid_y - chart_top        # rows above midline
    tx_rows = chart_bot - mid_y        # rows below midline

    if rx_rows < 2 or tx_rows < 2:
        safe_addstr(stdscr, 2, 2, "Window too small", dim)
        stdscr.refresh()
        return

    # ── Find peak rate for scaling ──────────────────────────
    peak_rx = 1024  # minimum 1 KB/s scale
    peak_tx = 1024
    if history:
        for e in history:
            if e.get("rx", 0) > peak_rx:
                peak_rx = e["rx"]
            if e.get("tx", 0) > peak_tx:
                peak_tx = e["tx"]
    # Add 20% headroom
    peak_rx *= 1.2
    peak_tx *= 1.2

    # ── Y-axis labels + grid ──────────────────────────────
    # RX labels (top half)
    for row_idx in range(rx_rows):
        screen_y = chart_top + row_idx
        frac = 1.0 - (row_idx / max(rx_rows - 1, 1))
        rate_at_row = peak_rx * frac

        # Label at top, middle, bottom of RX section
        if row_idx == 0 or row_idx == rx_rows - 1 or (rx_rows > 4 and row_idx == rx_rows // 2):
            label = f"{format_rate(rate_at_row):>8}"
            safe_addstr(stdscr, screen_y, margin_x, label, curses.color_pair(C_RX))

        safe_addstr(stdscr, screen_y, plot_x - 1, "│", dim)
        for cx in range(plot_w):
            if cx % 12 == 0:
                safe_addstr(stdscr, screen_y, plot_x + cx, "·", dim)

    # Center axis
    safe_addstr(stdscr, mid_y, margin_x, "   0    ", dim)
    safe_addstr(stdscr, mid_y, plot_x - 1, "├" + "─" * plot_w, dim)

    # TX labels (bottom half)
    for row_idx in range(tx_rows):
        screen_y = mid_y + 1 + row_idx
        frac = (row_idx + 1) / max(tx_rows, 1)
        rate_at_row = peak_tx * frac

        if row_idx == tx_rows - 1 or (tx_rows > 4 and row_idx == tx_rows // 2):
            label = f"{format_rate(rate_at_row):>8}"
            safe_addstr(stdscr, screen_y, margin_x, label, curses.color_pair(C_TX))

        safe_addstr(stdscr, screen_y, plot_x - 1, "│", dim)
        for cx in range(plot_w):
            if cx % 12 == 0:
                safe_addstr(stdscr, screen_y, plot_x + cx, "·", dim)

    # Bottom axis
    axis_y = chart_bot + 1
    safe_addstr(stdscr, axis_y, plot_x - 1, "└" + "─" * plot_w, dim)

    # ── Plot data ───────────────────────────────────────────
    if history and len(history) >= 2:
        t_min = history[0]["t"]
        t_max = history[-1]["t"]
        t_span = max(t_max - t_min, 1)

        # Bucket into columns
        bucket_rx = [None] * plot_w
        bucket_tx = [None] * plot_w

        for entry in history:
            frac = (entry["t"] - t_min) / t_span
            col = int(frac * (plot_w - 1))
            col = max(0, min(col, plot_w - 1))
            if bucket_rx[col] is None:
                bucket_rx[col] = []
                bucket_tx[col] = []
            bucket_rx[col].append(entry.get("rx", 0))
            bucket_tx[col].append(entry.get("tx", 0))

        # Average buckets
        rx_vals = [None] * plot_w
        tx_vals = [None] * plot_w
        for i in range(plot_w):
            if bucket_rx[i] is not None:
                rx_vals[i] = max(bucket_rx[i])  # use peak, not average, for visual pop
            if bucket_tx[i] is not None:
                tx_vals[i] = max(bucket_tx[i])

        # Draw RX bars (upward from midline)
        rx_sub_total = rx_rows * 8
        for cx in range(plot_w):
            val = rx_vals[cx]
            if val is None or val <= 0:
                continue
            fill_frac = min(val / peak_rx, 1.0)
            fill_sub = int(fill_frac * rx_sub_total)
            fill_sub = max(0, min(fill_sub, rx_sub_total))
            full_rows = fill_sub // 8
            remainder = fill_sub % 8

            for row_from_mid in range(rx_rows):
                screen_y = mid_y - 1 - row_from_mid
                if screen_y < chart_top:
                    break
                if row_from_mid < full_rows:
                    safe_addstr(stdscr, screen_y, plot_x + cx, "█", rx_color)
                elif row_from_mid == full_rows and remainder > 0:
                    safe_addstr(stdscr, screen_y, plot_x + cx, BLOCKS_UP[remainder], rx_color)

        # Draw TX bars (downward from midline)
        tx_sub_total = tx_rows * 8
        for cx in range(plot_w):
            val = tx_vals[cx]
            if val is None or val <= 0:
                continue
            fill_frac = min(val / peak_tx, 1.0)
            fill_sub = int(fill_frac * tx_sub_total)
            fill_sub = max(0, min(fill_sub, tx_sub_total))
            full_rows = fill_sub // 8
            remainder = fill_sub % 8

            for row_from_mid in range(tx_rows):
                screen_y = mid_y + 1 + row_from_mid
                if screen_y > chart_bot:
                    break
                if row_from_mid < full_rows:
                    safe_addstr(stdscr, screen_y, plot_x + cx, "█", tx_color)
                elif row_from_mid == full_rows and remainder > 0:
                    # Top-aligned partial block for downward bars
                    blocks_dn = [" ", "▔", "▔", "▀", "▀", "▀", "▓", "▓", "█"]
                    safe_addstr(stdscr, screen_y, plot_x + cx, blocks_dn[remainder], tx_color)

        # Time labels
        time_y = axis_y + 1
        for frac, align in [(0.0, "left"), (0.5, "center"), (1.0, "right")]:
            t_val = t_min + t_span * frac
            label = time.strftime("%H:%M:%S", time.localtime(t_val))
            cx = int(frac * (plot_w - 1))
            if align == "center":
                cx = max(0, cx - len(label) // 2)
            elif align == "right":
                cx = max(0, cx - len(label) + 1)
            safe_addstr(stdscr, time_y, plot_x + cx, label, dim)

    else:
        msg = "Collecting data... chart appears after a few seconds."
        safe_addstr(stdscr, mid_y, plot_x + 2, msg, dim)

    # ── Stats footer ────────────────────────────────────────
    stat_y = h - 2
    sep_w = w - 4
    safe_addstr(stdscr, stat_y - 1, 1, "─" * sep_w, dim)

    # Line 1: current rates + session totals
    rx_bps = stats.get("rx_bps", 0)
    tx_bps = stats.get("tx_bps", 0)
    rx_sess = stats.get("rx_session", 0)
    tx_sess = stats.get("tx_session", 0)

    safe_addstr(stdscr, stat_y, 2, "▼ ", rx_color)
    safe_addstr(stdscr, stat_y, 4, f"RX: {format_rate(rx_bps):>10}  ({format_bytes(rx_sess)} this session)", curses.color_pair(C_RX))

    tx_x = max(w // 2, 45)
    safe_addstr(stdscr, stat_y, tx_x, "▲ ", tx_color)
    safe_addstr(stdscr, stat_y, tx_x + 2, f"TX: {format_rate(tx_bps):>10}  ({format_bytes(tx_sess)} this session)", curses.color_pair(C_TX))

    # Footer line
    footer_y = h - 1
    ts = time.strftime("%H:%M:%S", time.localtime(last_update))
    n = len(history)
    iface_list = ",".join(interfaces[:4])
    footer = f" {ts} · {n} samples · [{interface}] · [i]face [r]efresh [c]lear [q]uit"
    safe_addstr(stdscr, footer_y, 1, footer, dim)

    stdscr.refresh()


# ── Main ────────────────────────────────────────────────────────────────────

def main(stdscr):
    curses.curs_set(0)
    stdscr.timeout(300)
    init_colors()

    interfaces = get_interfaces()
    iface_idx = 0
    interface = interfaces[iface_idx] if interfaces else "en0"

    tracker = RateTracker()
    history = load_history()
    last_save = time.time()
    last_refresh = 0
    refresh_interval = 2
    stats = {}

    while True:
        now = time.time()

        if now - last_refresh >= refresh_interval:
            rx_bytes, tx_bytes = get_interface_bytes(interface)
            stats = tracker.update(rx_bytes, tx_bytes)
            last_refresh = now

            if record_sample(history, stats):
                if now - last_save > 60 or len(history) < 5:
                    save_history(history)
                    last_save = now

        if stats:
            draw(stdscr, stats, last_refresh, history, interface, interfaces)

        key = stdscr.getch()
        if key == ord("q") or key == ord("Q"):
            save_history(history)
            break
        elif key == ord("r") or key == ord("R"):
            last_refresh = 0
        elif key == ord("c") or key == ord("C"):
            history.clear()
            save_history(history)
        elif key == ord("i") or key == ord("I"):
            # Cycle through interfaces
            interfaces = get_interfaces()
            if interfaces:
                iface_idx = (iface_idx + 1) % len(interfaces)
                interface = interfaces[iface_idx]
                tracker.reset()
                history.clear()
        elif key == curses.KEY_RESIZE:
            stdscr.clear()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    import platform
    if platform.system() != "Darwin":
        print("⚠️  netop is designed for macOS.")
        print("   Running anyway for demo purposes...\n")

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        save_history(load_history())
