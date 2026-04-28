"""
main.py - Claude Code Usage Monitor Widget (Windows desktop, tkinter)
"""

import sys
import os
import json
import logging
import threading
import queue
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from usage_monitor import calculate_metrics, UsageMetrics, PLAN_SESSION_LIMITS
from database import UsageDatabase
from optimization import get_best_worst_times, get_current_slot_rank, get_avoid_times
from claude_api import get_live_usage, save_session, load_session

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / ".claude-usage-widget"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "widget.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
DB_PATH     = SCRIPT_DIR / "usage_history.db"

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "plan": "pro",
    "custom_session_limit": None,
    "custom_weekly_limit": None,
    "refresh_interval_seconds": 60,
    "window_x": 100,
    "window_y": 100,
    "opacity": 0.92,
    "always_on_top": True,
    "alert_thresholds": [75, 90, 95],
    "enable_audio_alerts": False,
    "data_retention_days": 90,
    "custom_data_path": None,
    "theme": "dark",
}

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception as e:
            logger.warning("Could not load config: %s", e)
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error("Could not save config: %s", e)

# ── Themes / colours ──────────────────────────────────────────────────────────
THEMES = {
    "dark": {
        "bg": "#1a1a2e", "fg": "#e0e0e0", "title_fg": "#a0c4ff",
        "border": "#2a2a4a", "bar_bg": "#2a2a4a",
        "btn_bg": "#2a2a4a", "btn_fg": "#a0c4ff",
        "error_fg": "#ff6b6b", "dim_fg": "#888888",
    },
    "light": {
        "bg": "#f5f5f5", "fg": "#222222", "title_fg": "#1a73e8",
        "border": "#dddddd", "bar_bg": "#cccccc",
        "btn_bg": "#e0e0e0", "btn_fg": "#1a73e8",
        "error_fg": "#cc0000", "dim_fg": "#666666",
    },
}

USAGE_COLORS = {
    "green":  "#4caf50",
    "yellow": "#ffc107",
    "orange": "#ff9800",
    "red":    "#f44336",
}

def pct_color(pct: float) -> str:
    if pct <= 50:   return USAGE_COLORS["green"]
    if pct <= 75:   return USAGE_COLORS["yellow"]
    if pct <= 90:   return USAGE_COLORS["orange"]
    return USAGE_COLORS["red"]

# ── Progress bar widget ───────────────────────────────────────────────────────
class ProgressBar(tk.Canvas):
    def __init__(self, parent, width=170, height=10, bar_bg="#2a2a4a", **kw):
        super().__init__(parent, width=width, height=height,
                         bg=bar_bg, highlightthickness=0, **kw)
        # Use _pw/_ph to avoid clobbering tkinter's internal _w (widget path)
        self._pw, self._ph, self._bar_bg = width, height, bar_bg

    def set_value(self, pct: float, color: str):
        self.delete("all")
        self.create_rectangle(0, 0, self._pw, self._ph, fill=self._bar_bg, outline="")
        fill_w = int(self._pw * min(max(pct, 0), 100) / 100)
        if fill_w > 0:
            self.create_rectangle(0, 0, fill_w, self._ph, fill=color, outline="")

# ── Main widget ───────────────────────────────────────────────────────────────
class ClaudeUsageWidget:
    def __init__(self):
        self.config   = load_config()
        self.db       = UsageDatabase(DB_PATH)
        self.metrics  = None
        self._alerted = set()
        self._prev_session_tokens: Optional[int] = None
        self._prev_session_pct: Optional[float] = None
        self._prev_weekly_tokens: Optional[int] = None
        self._prev_weekly_pct: Optional[float] = None
        self._last_multiplier: Optional[tuple] = None
        self._opt_visible = False
        self._collapsed = False
        self._drag_x = self._drag_y = 0
        self._queue  = queue.Queue()

        self.root = tk.Tk()
        self._setup_window()
        self._build_ui()
        self._start_tray()
        self._schedule_update()

    # ── Window ────────────────────────────────────────────────────────────────
    def _setup_window(self):
        self.root.title("Claude Usage Monitor")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", self.config.get("always_on_top", True))
        self.root.attributes("-alpha",   self.config.get("opacity", 0.92))
        self.root.resizable(False, False)
        x, y = self.config.get("window_x", 100), self.config.get("window_y", 100)
        self.root.geometry(f"+{x}+{y}")
        self.root.configure(bg=self._t("bg"))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _t(self, key: str) -> str:
        """Look up a colour from the active theme."""
        name = self.config.get("theme", "dark")
        return THEMES.get(name, THEMES["dark"]).get(key, "#ffffff")

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        bg = self._t("bg")

        # Title bar (drag handle)
        self.title_bar = tk.Frame(self.root, bg=self._t("border"), height=28)
        self.title_bar.pack(fill=tk.X)
        for w in (self.title_bar,):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>",     self._drag_move)

        title_lbl = tk.Label(self.title_bar, text="☁ Claude Usage",
                             bg=self._t("border"), fg=self._t("title_fg"),
                             font=("Segoe UI", 9, "bold"), pady=4)
        title_lbl.pack(side=tk.LEFT, padx=8)
        title_lbl.bind("<ButtonPress-1>", self._drag_start)
        title_lbl.bind("<B1-Motion>",     self._drag_move)

        tk.Label(self.title_bar, text="✕", bg=self._t("border"),
                 fg=self._t("dim_fg"), font=("Segoe UI", 9),
                 padx=8, pady=4, cursor="hand2").pack(side=tk.RIGHT) \
            .__class__  # bind after packing
        close_lbl = self.title_bar.winfo_children()[-1]
        close_lbl.bind("<Button-1>", lambda e: self._on_close())

        gear_lbl = tk.Label(self.title_bar, text="⚙", bg=self._t("border"),
                            fg=self._t("dim_fg"), font=("Segoe UI", 9),
                            padx=4, pady=4, cursor="hand2")
        gear_lbl.pack(side=tk.RIGHT)
        gear_lbl.bind("<Button-1>", lambda e: self._open_settings())

        self.collapse_lbl = tk.Label(self.title_bar, text="−", bg=self._t("border"),
                                     fg=self._t("dim_fg"), font=("Segoe UI", 9),
                                     padx=4, pady=4, cursor="hand2")
        self.collapse_lbl.pack(side=tk.RIGHT)
        self.collapse_lbl.bind("<Button-1>", lambda e: self._toggle_collapse())

        # Body
        self.body = tk.Frame(self.root, bg=bg, padx=12, pady=8)
        self.body.pack(fill=tk.BOTH, expand=True)
        body = self.body

        # 5-hour row
        row5 = tk.Frame(body, bg=bg); row5.pack(fill=tk.X, pady=(0, 4))
        tk.Label(row5, text="5hr:", bg=bg, fg=self._t("fg"),
                 font=("Consolas", 9), width=5, anchor="w").pack(side=tk.LEFT)
        self.bar_5hr = ProgressBar(row5, bar_bg=self._t("bar_bg"))
        self.bar_5hr.pack(side=tk.LEFT, padx=(2, 4))
        self.lbl_5hr = tk.Label(row5, text="  0%", bg=bg,
                                fg=USAGE_COLORS["green"],
                                font=("Consolas", 9, "bold"), width=5, anchor="w")
        self.lbl_5hr.pack(side=tk.LEFT)

        # Weekly row
        rowW = tk.Frame(body, bg=bg); rowW.pack(fill=tk.X, pady=(0, 6))
        tk.Label(rowW, text="Week:", bg=bg, fg=self._t("fg"),
                 font=("Consolas", 9), width=5, anchor="w").pack(side=tk.LEFT)
        self.bar_week = ProgressBar(rowW, bar_bg=self._t("bar_bg"))
        self.bar_week.pack(side=tk.LEFT, padx=(2, 4))
        self.lbl_week = tk.Label(rowW, text="  0%", bg=bg,
                                 fg=USAGE_COLORS["green"],
                                 font=("Consolas", 9, "bold"), width=5, anchor="w")
        self.lbl_week.pack(side=tk.LEFT)

        tk.Frame(body, bg=self._t("border"), height=1).pack(fill=tk.X, pady=4)

        # Burn rate
        brow = tk.Frame(body, bg=bg); brow.pack(fill=tk.X)
        tk.Label(brow, text="Burn:", bg=bg, fg=self._t("dim_fg"),
                 font=("Consolas", 8)).pack(side=tk.LEFT)
        self.lbl_burn = tk.Label(brow, text="-- tok/min", bg=bg,
                                 fg=self._t("fg"), font=("Consolas", 8))
        self.lbl_burn.pack(side=tk.LEFT, padx=4)
        self.lbl_mult = tk.Label(brow, text="", bg=bg,
                                 fg="#ff8c00", font=("Consolas", 8))
        self.lbl_mult.pack(side=tk.LEFT)

        # ETA
        erow = tk.Frame(body, bg=bg); erow.pack(fill=tk.X)
        tk.Label(erow, text="ETA: ", bg=bg, fg=self._t("dim_fg"),
                 font=("Consolas", 8)).pack(side=tk.LEFT)
        self.lbl_eta = tk.Label(erow, text="--", bg=bg,
                                fg=self._t("fg"), font=("Consolas", 8))
        self.lbl_eta.pack(side=tk.LEFT)

        # Status line
        self.lbl_status = tk.Label(body, text="Initialising...",
                                   bg=bg, fg=self._t("dim_fg"),
                                   font=("Consolas", 7), anchor="w", wraplength=240)
        self.lbl_status.pack(fill=tk.X, pady=(4, 0))

        # Optimisation toggle
        self.opt_btn = tk.Button(body, text="\U0001f4ca Show Optimisation",
                                 bg=self._t("btn_bg"), fg=self._t("btn_fg"),
                                 font=("Segoe UI", 8), relief="flat",
                                 cursor="hand2", command=self._toggle_opt)
        self.opt_btn.pack(fill=tk.X, pady=(6, 2))

        # Optimisation panel (hidden)
        self.opt_frame = tk.Frame(self.root, bg=bg)
        self._build_opt_panel()

    def _build_opt_panel(self):
        bg = self._t("bg")
        tk.Frame(self.opt_frame, bg=self._t("border"), height=1).pack(fill=tk.X)
        inner = tk.Frame(self.opt_frame, bg=bg, padx=12, pady=6)
        inner.pack(fill=tk.BOTH, expand=True)

        tk.Label(inner, text="Best Times (lowest burn):",
                 bg=bg, fg=self._t("dim_fg"),
                 font=("Consolas", 7, "bold")).pack(anchor="w")
        self.lbl_best = []
        for i in range(5):
            lbl = tk.Label(inner, text=f"  {i+1}. --", bg=bg,
                           fg=USAGE_COLORS["green"], font=("Consolas", 7), anchor="w")
            lbl.pack(fill=tk.X)
            self.lbl_best.append(lbl)

        tk.Frame(inner, bg=self._t("border"), height=1).pack(fill=tk.X, pady=4)

        tk.Label(inner, text="Worst Times (highest burn):",
                 bg=bg, fg=self._t("dim_fg"),
                 font=("Consolas", 7, "bold")).pack(anchor="w")
        self.lbl_worst = []
        for i in range(5):
            lbl = tk.Label(inner, text=f"  {i+1}. --", bg=bg,
                           fg=USAGE_COLORS["red"], font=("Consolas", 7), anchor="w")
            lbl.pack(fill=tk.X)
            self.lbl_worst.append(lbl)

        tk.Frame(inner, bg=self._t("border"), height=1).pack(fill=tk.X, pady=4)

        tk.Label(inner, text="Avoid (high multiplier ≥1.5x):",
                 bg=bg, fg=self._t("dim_fg"),
                 font=("Consolas", 7, "bold")).pack(anchor="w")
        self.lbl_avoid = []
        for i in range(5):
            lbl = tk.Label(inner, text=f"  {i+1}. --", bg=bg,
                           fg="#ff8c00", font=("Consolas", 7), anchor="w")
            lbl.pack(fill=tk.X)
            self.lbl_avoid.append(lbl)

        self.lbl_rank = tk.Label(inner, text="", bg=bg,
                                 fg=self._t("title_fg"),
                                 font=("Consolas", 7, "italic"))
        self.lbl_rank.pack(anchor="w", pady=(4, 0))

        tk.Button(inner, text="\U0001f4be Export CSV",
                  bg=self._t("btn_bg"), fg=self._t("btn_fg"),
                  font=("Segoe UI", 7), relief="flat", cursor="hand2",
                  command=self._export_csv).pack(fill=tk.X, pady=(4, 0))

    # ── Drag ─────────────────────────────────────────────────────────────────
    def _drag_start(self, e):
        self._drag_x = e.x_root - self.root.winfo_x()
        self._drag_y = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    def _toggle_collapse(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.body.pack_forget()
            self.opt_frame.pack_forget()
            self.collapse_lbl.config(text="+")
        else:
            self.body.pack(fill=tk.BOTH, expand=True)
            self.collapse_lbl.config(text="−")
            if self._opt_visible:
                self.opt_frame.pack(fill=tk.BOTH, expand=True)

    # ── Update loop ───────────────────────────────────────────────────────────
    def _schedule_update(self):
        interval_ms = int(self.config.get("refresh_interval_seconds", 10) * 1000)
        self._fetch_metrics()
        self.root.after(interval_ms, self._schedule_update)

    def _fetch_metrics(self):
        """Run metric calculation in a background thread; post result to main thread."""
        def worker():
            plan = self.config.get("plan", "pro")
            sess_lim = (
                self.config.get("custom_session_limit")
                or PLAN_SESSION_LIMITS.get(plan, 19_000)
            )
            week_lim = (
                self.config.get("custom_weekly_limit")
                or int(sess_lim * (7 * 24 / 5))
            )
            m = calculate_metrics(
                sess_lim, week_lim, self.config.get("custom_data_path")
            )

            # Overlay official percentages and reset times from claude.ai API
            live = get_live_usage()
            if live and not live.error:
                m.session_pct = live.session_pct
                m.weekly_pct  = live.weekly_pct
                m.current_session_end = live.session_resets_at
                # Recalculate ETA from official reset time
                if live.session_resets_at:
                    now = datetime.now(timezone.utc)
                    remaining = (live.session_resets_at - now).total_seconds() / 60
                    m.session_remaining_minutes = max(0, remaining)
                m.error = None

            self._queue.put(m)
            self.root.after(0, self._apply_metrics)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_metrics(self):
        try:
            m = self._queue.get_nowait()
        except queue.Empty:
            return

        # Burn rate from JSONL inp+out over the last 15 minutes of actual calls.
        # Idle time is naturally excluded — only real API activity counts.
        now = datetime.now(timezone.utc)
        if m.active_session:
            cutoff = now - timedelta(minutes=15)
            recent_entries = [
                e for e in m.active_session.entries
                if e.timestamp >= cutoff
            ]
            if len(recent_entries) >= 2:
                window_tokens = sum(e.input_tokens + e.output_tokens for e in recent_entries)
                oldest = min(e.timestamp for e in recent_entries)
                window_min = (now - oldest).total_seconds() / 60
                if window_min >= 2.0:
                    m.burn_rate_per_min = window_tokens / window_min

        self.metrics = m
        self._refresh_display(m)
        self._save_snapshot(m)
        self._log_multiplier(m)
        self._check_alerts(m)
        if self._opt_visible:
            self._refresh_opt()

    # ── Display refresh ───────────────────────────────────────────────────────
    def _refresh_display(self, m: UsageMetrics):
        c5 = pct_color(m.session_pct)
        self.bar_5hr.set_value(m.session_pct, c5)
        self.lbl_5hr.config(text=f"{m.session_pct:3.0f}%", fg=c5)

        cw = pct_color(m.weekly_pct)
        self.bar_week.set_value(m.weekly_pct, cw)
        self.lbl_week.config(text=f"{m.weekly_pct:3.0f}%", fg=cw)

        if m.burn_rate_per_min > 0:
            self.lbl_burn.config(text=f"{m.burn_rate_per_min:,.0f} tok/min")
        else:
            self.lbl_burn.config(text="-- tok/min")
        if self._last_multiplier is not None:
            s_mult, w_mult = self._last_multiplier
            color = "#ff8c00" if max(s_mult, w_mult) >= 1.5 else "#aaaaaa"
            self.lbl_mult.config(text=f"(5h:{s_mult:.1f}x wk:{w_mult:.1f}x)", fg=color)
        else:
            self.lbl_mult.config(text="")

        if m.session_remaining_minutes is not None:
            mins = int(m.session_remaining_minutes)
            eta = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
            self.lbl_eta.config(text=f"Resets in {eta}")
        else:
            self.lbl_eta.config(text="--")

        if m.error:
            self.lbl_status.config(text=m.error, fg=self._t("error_fg"))
        elif m.data_path:
            ts = datetime.now().strftime("%H:%M:%S")
            self.lbl_status.config(
                text=f"Updated {ts}  |  {m.weekly_tokens:,} tok/wk",
                fg=self._t("dim_fg"),
            )
        else:
            self.lbl_status.config(text="No data found", fg=self._t("error_fg"))

    def _save_snapshot(self, m: UsageMetrics):
        try:
            if m.burn_rate_per_min > 0:
                self.db.save_snapshot(m.session_tokens, m.burn_rate_per_min)
        except Exception as e:
            logger.error("snapshot save failed: %s", e)

    def _log_multiplier(self, m: UsageMetrics):
        """Compare token delta vs pct delta to derive the Anthropic cost multiplier for both windows."""
        try:
            prev_s_tok = self._prev_session_tokens
            prev_s_pct = self._prev_session_pct
            prev_w_tok = self._prev_weekly_tokens
            prev_w_pct = self._prev_weekly_pct

            self._prev_session_tokens = m.session_tokens
            self._prev_session_pct    = m.session_pct
            self._prev_weekly_tokens  = m.weekly_tokens
            self._prev_weekly_pct     = m.weekly_pct

            if any(v is None for v in (prev_s_tok, prev_s_pct, prev_w_tok, prev_w_pct)):
                return

            tokens_delta   = m.session_tokens - prev_s_tok
            s_pct_delta    = m.session_pct    - prev_s_pct
            w_pct_delta    = m.weekly_pct     - prev_w_pct

            # Need a meaningful token increase and both pct values moving up
            if tokens_delta < 100 or s_pct_delta <= 0 or w_pct_delta <= 0:
                return

            def _multiplier(pct_delta: float, limit: int) -> Optional[float]:
                if limit <= 0:
                    return None
                expected = (tokens_delta / limit) * 100
                if expected <= 0:
                    return None
                m = pct_delta / expected
                return m if 0.1 <= m <= 50.0 else None

            s_mult = _multiplier(s_pct_delta, m.session_limit)
            w_mult = _multiplier(w_pct_delta, m.weekly_limit)

            if s_mult is None or w_mult is None:
                return

            model = ""
            if m.active_session and m.active_session.entries:
                model = m.active_session.entries[-1].model or ""

            self._last_multiplier = (s_mult, w_mult)
            self.db.save_multiplier(
                session_tokens=m.session_tokens,
                tokens_delta=tokens_delta,
                session_pct=m.session_pct,
                session_pct_delta=s_pct_delta,
                session_multiplier=s_mult,
                weekly_tokens=m.weekly_tokens,
                weekly_pct=m.weekly_pct,
                weekly_pct_delta=w_pct_delta,
                weekly_multiplier=w_mult,
                model=model,
            )
            logger.info(
                "Multiplier logged — 5hr: %.2fx  weekly: %.2fx  model: %s  (tokens_delta=%d)",
                s_mult, w_mult, model or "unknown", tokens_delta,
            )
        except Exception as e:
            logger.error("multiplier log failed: %s", e)

    # ── Alerts ────────────────────────────────────────────────────────────────
    def _check_alerts(self, m: UsageMetrics):
        for pct, label in [(m.session_pct, "5hr"), (m.weekly_pct, "Weekly")]:
            for threshold in self.config.get("alert_thresholds", [75, 90, 95]):
                key = f"{label}_{threshold}"
                if pct >= threshold and key not in self._alerted:
                    self._alerted.add(key)
                    self._notify(
                        "Claude Usage Alert",
                        f"{label} limit reached {pct:.0f}% (threshold {threshold}%)",
                    )
                elif pct < threshold * 0.9:
                    self._alerted.discard(key)

    def _notify(self, title: str, msg: str):
        try:
            from plyer import notification
            notification.notify(title=title, message=msg,
                                app_name="Claude Usage Monitor", timeout=8)
        except Exception:
            logger.info("Alert: %s – %s", title, msg)

    # ── Optimisation panel ────────────────────────────────────────────────────
    def _toggle_opt(self):
        self._opt_visible = not self._opt_visible
        if self._opt_visible:
            self.opt_frame.pack(fill=tk.BOTH, expand=True)
            self.opt_btn.config(text="\U0001f4ca Hide Optimisation")
            self._refresh_opt()
        else:
            self.opt_frame.pack_forget()
            self.opt_btn.config(text="\U0001f4ca Show Optimisation")

    def _refresh_opt(self):
        try:
            stats = self.db.get_hourly_stats()
            best, worst = get_best_worst_times(stats, top_n=5)

            for i, lbl in enumerate(self.lbl_best):
                if i < len(best):
                    s = best[i]
                    lbl.config(text=f"  {i+1}. {s.label} ({s.burn_display})")
                else:
                    lbl.config(text=f"  {i+1}. (collecting data…)")

            for i, lbl in enumerate(self.lbl_worst):
                if i < len(worst):
                    s = worst[i]
                    lbl.config(text=f"  {i+1}. {s.label} ({s.burn_display})")
                else:
                    lbl.config(text=f"  {i+1}. (collecting data…)")

            mult_stats = self.db.get_hourly_multiplier_stats()
            avoid = get_avoid_times(mult_stats, threshold=1.5, top_n=5)
            for i, lbl in enumerate(self.lbl_avoid):
                if i < len(avoid):
                    s = avoid[i]
                    lbl.config(text=f"  {i+1}. {s.label} ({s.multiplier_display})")
                else:
                    lbl.config(text=f"  {i+1}. (collecting data…)")

            now = datetime.now(timezone.utc)
            rank, total = get_current_slot_rank(stats, now.weekday(), now.hour)
            if rank and total:
                self.lbl_rank.config(text=f"Current slot: #{rank} of {total} ranked")
            else:
                self.lbl_rank.config(text="Current slot: no data yet")
        except Exception as e:
            logger.error("opt refresh failed: %s", e)

    # ── Settings dialog ───────────────────────────────────────────────────────
    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings – Claude Usage Monitor")
        win.configure(bg=self._t("bg"))
        win.attributes("-topmost", True)
        win.resizable(False, False)
        bg, fg, font = self._t("bg"), self._t("fg"), ("Segoe UI", 9)

        org_id, session_key = load_session()

        fields = [
            ("Plan (pro / max5 / max20 / custom):", "plan"),
            ("Refresh interval (seconds):",          "refresh_interval_seconds"),
            ("Opacity (0.1 – 1.0):",                 "opacity"),
            ("Custom data path (leave blank = auto):","custom_data_path"),
            ("Custom session limit (tokens):",        "custom_session_limit"),
            ("Custom weekly limit (tokens):",         "custom_weekly_limit"),
        ]
        vars_ = {}
        frm = tk.Frame(win, bg=bg, padx=14, pady=10)
        frm.pack()
        for r, (label, key) in enumerate(fields):
            tk.Label(frm, text=label, bg=bg, fg=fg, font=font, anchor="w") \
                .grid(row=r, column=0, sticky="w", padx=6, pady=3)
            v = tk.StringVar(value=str(self.config.get(key) or ""))
            tk.Entry(frm, textvariable=v, bg=self._t("border"), fg=fg,
                     font=font, insertbackground=fg, width=24) \
                .grid(row=r, column=1, padx=6, pady=3)
            vars_[key] = v

        # Live API credentials
        tk.Frame(frm, bg=self._t("border"), height=1).grid(
            row=len(fields), column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        tk.Label(frm, text="── Live API (claude.ai) ──",
                 bg=bg, fg=self._t("dim_fg"), font=("Segoe UI", 8)).grid(
            row=len(fields)+1, column=0, columnspan=2, pady=(0,4))
        for i, (label, val) in enumerate([
            ("Organisation ID:", org_id or ""),
            ("Session Key:",     session_key or ""),
        ]):
            tk.Label(frm, text=label, bg=bg, fg=fg, font=font, anchor="w").grid(
                row=len(fields)+2+i, column=0, sticky="w", padx=6, pady=3)
        org_var = tk.StringVar(value=org_id or "")
        ses_var = tk.StringVar(value=session_key or "")
        tk.Entry(frm, textvariable=org_var, bg=self._t("border"), fg=fg,
                 font=font, insertbackground=fg, width=24).grid(
            row=len(fields)+2, column=1, padx=6, pady=3)
        tk.Entry(frm, textvariable=ses_var, bg=self._t("border"), fg=fg,
                 font=font, insertbackground=fg, width=24, show="*").grid(
            row=len(fields)+3, column=1, padx=6, pady=3)

        aot_var = tk.BooleanVar(value=self.config.get("always_on_top", True))
        tk.Checkbutton(frm, text="Always on top", variable=aot_var,
                       bg=bg, fg=fg, selectcolor=bg, font=font,
                       activebackground=bg, activeforeground=fg) \
            .grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=6, pady=3)

        theme_var = tk.StringVar(value=self.config.get("theme", "dark"))
        thm = tk.Frame(frm, bg=bg)
        thm.grid(row=len(fields)+1, column=0, columnspan=2, sticky="w", padx=6)
        tk.Label(thm, text="Theme:", bg=bg, fg=fg, font=font).pack(side=tk.LEFT)
        for t in ("dark", "light"):
            tk.Radiobutton(thm, text=t, variable=theme_var, value=t,
                           bg=bg, fg=fg, selectcolor=bg, font=font,
                           activebackground=bg).pack(side=tk.LEFT, padx=4)

        def _save():
            try:
                self.config["plan"] = vars_["plan"].get().strip().lower() or "pro"
                self.config["refresh_interval_seconds"] = float(
                    vars_["refresh_interval_seconds"].get() or 10)
                self.config["opacity"] = float(vars_["opacity"].get() or 0.92)
                self.config["always_on_top"] = aot_var.get()
                self.config["theme"] = theme_var.get()
                p = vars_["custom_data_path"].get().strip()
                self.config["custom_data_path"] = p or None
                sl = vars_["custom_session_limit"].get().strip()
                self.config["custom_session_limit"] = int(sl) if sl else None
                wl = vars_["custom_weekly_limit"].get().strip()
                self.config["custom_weekly_limit"] = int(wl) if wl else None
                save_config(self.config)
                self.root.attributes("-alpha",   self.config["opacity"])
                self.root.attributes("-topmost", self.config["always_on_top"])
                oid = org_var.get().strip()
                sk  = ses_var.get().strip()
                if oid and sk:
                    save_session(oid, sk)
            except Exception as e:
                logger.error("Settings save error: %s", e)
            win.destroy()

        def _make_shortcut():
            create_desktop_shortcut()
            self._notify("Shortcut created", "Claude Usage Monitor shortcut added to Desktop")

        tk.Button(frm, text="Create Desktop Shortcut",
                  bg=self._t("btn_bg"), fg=self._t("btn_fg"),
                  font=font, relief="flat", cursor="hand2",
                  command=_make_shortcut).grid(
            row=len(fields)+2, column=0, columnspan=2,
            sticky="ew", padx=6, pady=(8, 2))

        tk.Button(frm, text="Save & Close", bg=self._t("btn_bg"),
                  fg=self._t("btn_fg"), font=font, relief="flat", cursor="hand2",
                  command=_save).grid(
            row=len(fields)+3, column=0, columnspan=2,
            sticky="ew", padx=6, pady=(2, 8))

    # ── Export ────────────────────────────────────────────────────────────────
    def _export_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            title="Export usage history",
        )
        if path:
            try:
                self.db.export_csv(Path(path))
                self._notify("Export complete", f"Saved to {path}")
            except Exception as e:
                logger.error("Export failed: %s", e)

    # ── System tray ───────────────────────────────────────────────────────────
    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse([4, 4, 60, 60], fill=(26, 26, 46, 230))
            d.ellipse([14, 14, 50, 50], fill=(100, 196, 255, 200))

            def _show(icon, _):
                self.root.after(0, self.root.deiconify)

            def _quit(icon, _):
                icon.stop()
                self.root.after(0, self._on_close)

            icon = pystray.Icon(
                "claude_usage", img, "Claude Usage Monitor",
                menu=pystray.Menu(
                    pystray.MenuItem("Show", _show, default=True),
                    pystray.MenuItem("Quit", _quit),
                ),
            )
            threading.Thread(target=icon.run, daemon=True).start()
            self._tray_icon = icon
        except ImportError:
            logger.warning("pystray/Pillow not installed – tray disabled")
        except Exception as e:
            logger.warning("Tray init failed: %s", e)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def _on_close(self):
        try:
            self.config["window_x"] = self.root.winfo_x()
            self.config["window_y"] = self.root.winfo_y()
            save_config(self.config)
        except Exception:
            pass
        try:
            if hasattr(self, "_tray_icon"):
                self._tray_icon.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ── Desktop shortcut ─────────────────────────────────────────────────────────
def create_desktop_shortcut():
    """Create a .lnk on the user's Desktop via Windows Script Host (no extra deps)."""
    import subprocess
    import tempfile

    script_path = Path(__file__).resolve()
    python_exe  = Path(sys.executable)
    # pythonw.exe suppresses the console window
    pythonw_exe = python_exe.parent / "pythonw.exe"
    if not pythonw_exe.exists():
        pythonw_exe = python_exe

    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        # OneDrive-roamed desktop
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "OneDrive" / "Desktop"
    if not desktop.exists():
        desktop = Path.home()

    lnk_path = desktop / "Claude Usage Monitor.lnk"

    q = '"'
    vbs = (
        f'Set sh = CreateObject("WScript.Shell")\n'
        f'Set lnk = sh.CreateShortcut("{lnk_path}")\n'
        f'lnk.TargetPath = "{pythonw_exe}"\n'
        f'lnk.Arguments = {q}{script_path}{q}\n'
        f'lnk.WorkingDirectory = "{script_path.parent}"\n'
        f'lnk.Description = "Claude Code Usage Monitor"\n'
        f'lnk.Save\n'
    )

    with tempfile.NamedTemporaryFile(suffix=".vbs", mode="w",
                                     delete=False, encoding="utf-8") as f:
        f.write(vbs)
        vbs_path = f.name

    try:
        result = subprocess.run(
            ["cscript", "//NoLogo", vbs_path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            print(f"Shortcut created: {lnk_path}")
        else:
            print(f"Failed to create shortcut: {result.stderr.strip()}")
    finally:
        try:
            os.unlink(vbs_path)
        except OSError:
            pass


# ── Auto-start (Windows registry) ────────────────────────────────────────────
def set_autostart(enable: bool):
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        name = "ClaudeUsageMonitor"
        if enable:
            cmd = f'"{sys.executable}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        print(f"Auto-start {'enabled' if enable else 'disabled'}.")
    except ImportError:
        print("winreg not available – not running on Windows.")
    except Exception as e:
        print(f"Auto-start error: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Claude Code Usage Monitor Widget")
    ap.add_argument("--autostart",    action="store_true", help="Enable Windows auto-start")
    ap.add_argument("--no-autostart", action="store_true", help="Disable Windows auto-start")
    ap.add_argument("--shortcut",     action="store_true", help="Create desktop shortcut and exit")
    args = ap.parse_args()

    if args.autostart:
        set_autostart(True); sys.exit(0)
    if args.no_autostart:
        set_autostart(False); sys.exit(0)
    if args.shortcut:
        create_desktop_shortcut(); sys.exit(0)

    ClaudeUsageWidget().run()
