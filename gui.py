"""
Pizzatron 3000 Bot — GUI
=========================
Tkinter interface with background artwork, floating dashboard cards,
Start / Pause / Stop buttons, activity log, and calibration wizard.

Usage:
    python gui.py              # Development
    Pizzatron3000Bot.exe       # Built executable
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, messagebox
from typing import Optional

import pyautogui
from PIL import Image, ImageTk, ImageEnhance


# ── Path helpers (work both in dev and PyInstaller .exe) ─────────
def _resource(rel: str) -> str:
    """Path to a bundled resource (inside .exe or next to script)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _appdir(rel: str) -> str:
    """Path relative to the running exe / script directory (user data)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def get_config_path() -> str:
    """Find existing config.json across appdir, scriptdir, and cwd."""
    candidates = [
        _appdir("config.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"),
        os.path.join(os.getcwd(), "config.json"),
        os.path.join(os.getcwd(), "dist", "config.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return _appdir("config.json")


CONFIG_FILE = get_config_path()
BG_IMAGE = _resource(os.path.join("assets", "background.png"))
DISCORD_HANDLE = "@salemcorpse"

# Import bot core
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pizza_bot import OrderDetectionError, PizzaBot  # noqa: E402


# ═════════════════════════════════════════════════════════════════
#  Design tokens
# ═════════════════════════════════════════════════════════════════
WIN_W, WIN_H = 560, 680
CARD         = "#1a1a2e"
CARD_LIGHT   = "#242440"
FG           = "#f0f0f0"
FG_DIM       = "#9090b0"
GREEN        = "#a6e3a1"
YELLOW       = "#f9e2af"
RED          = "#f38ba8"
BLUE         = "#89b4fa"
ACCENT       = "#f38ba8"


# ═════════════════════════════════════════════════════════════════
#  Calibration Wizard
# ═════════════════════════════════════════════════════════════════
CALIB_STEPS = [
    ("game_top_left",     "Top-left corner of the GAME area"),
    ("game_bottom_right", "Bottom-right corner of the GAME area"),
    ("order_top_left",    "Top-left of the ORDER BOARD (white rect)"),
    ("order_bottom_right","Bottom-right of the ORDER BOARD"),
    ("sauce_regular",     "REGULAR SAUCE bottle BODY / pickup point (orange)"),
    ("_sauce_regular_nozzle", "REGULAR SAUCE dispensing NOZZLE TIP"),
    ("sauce_hot",         "HOT SAUCE bottle BODY / pickup point (red)"),
    ("_sauce_hot_nozzle", "HOT SAUCE dispensing NOZZLE TIP"),
    ("topping_cheese",    "CHEESE bin (shredded yellow)"),
    ("topping_seaweed",   "SEAWEED bin (green)"),
    ("topping_shrimp",    "SHRIMP bin (pink)"),
    ("topping_squid",     "SQUID bin (blue)"),
    ("topping_fish",      "FISH bin (gray / silver)"),
    ("_conveyor",         "Center of CONVEYOR BELT"),
    ("_target",           "Ideal DROP POINT on conveyor"),
]


class CalibrationWizard(tk.Toplevel):
    """Step-through wizard: countdown → capture mouse position."""

    def __init__(self, parent: tk.Tk, on_done):
        super().__init__(parent)
        self.title("🔧  Calibration Wizard")
        self.configure(bg=CARD)
        self.geometry("480x290")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._on_done = on_done
        self._step = 0
        self._positions: dict = {}

        # Widgets
        self._lbl_step = tk.Label(self, text="", font=("Segoe UI", 11, "bold"),
                                  bg=CARD, fg=FG, wraplength=440, justify="center")
        self._lbl_step.pack(pady=(24, 4))

        self._lbl_instr = tk.Label(self, text="", font=("Segoe UI", 10),
                                   bg=CARD, fg=FG_DIM, wraplength=440, justify="center")
        self._lbl_instr.pack(pady=(0, 14))

        self._lbl_cd = tk.Label(self, text="", font=("Segoe UI", 30, "bold"),
                                bg=CARD, fg=ACCENT)
        self._lbl_cd.pack(pady=(0, 8))

        self._lbl_ok = tk.Label(self, text="", font=("Segoe UI", 10),
                                bg=CARD, fg=GREEN)
        self._lbl_ok.pack()

        bf = tk.Frame(self, bg=CARD)
        bf.pack(pady=(14, 16))
        self._btn_cap = tk.Button(bf, text="📍  Capture (3 s countdown)",
                                  font=("Segoe UI", 10, "bold"), bg=BLUE,
                                  fg="#1e1e2e", activebackground="#7aa2f7",
                                  relief="flat", padx=14, pady=5,
                                  command=self._start_cd)
        self._btn_cap.pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", font=("Segoe UI", 10), bg=CARD_LIGHT,
                  fg=FG, activebackground="#45475a", relief="flat", padx=10,
                  pady=5, command=self.destroy).pack(side="left", padx=6)

        self._show_step()

    def _show_step(self):
        key, desc = CALIB_STEPS[self._step]
        self._lbl_step.config(text=f"Step {self._step+1} / {len(CALIB_STEPS)}")
        self._lbl_instr.config(text=f"Move your mouse to:\n{desc}")
        self._lbl_cd.config(text="")
        self._lbl_ok.config(text="")
        self._btn_cap.config(state="normal")

    def _start_cd(self):
        self._btn_cap.config(state="disabled")
        self._lbl_ok.config(text="")
        self._tick(3)

    def _tick(self, n: int):
        if n > 0:
            self._lbl_cd.config(text=str(n))
            self.after(1000, self._tick, n - 1)
        else:
            self._lbl_cd.config(text="📸")
            self.after(120, self._capture)

    def _capture(self):
        pos = pyautogui.position()
        key, _ = CALIB_STEPS[self._step]
        self._positions[key] = [pos.x, pos.y]
        self._lbl_ok.config(text=f"✓  ({pos.x}, {pos.y})")
        self._lbl_cd.config(text="")
        self.after(600, self._advance)

    def _advance(self):
        self._step += 1
        if self._step < len(CALIB_STEPS):
            self._show_step()
        else:
            self._save()

    def _save(self):
        cfg: dict = {k: v for k, v in self._positions.items() if not k.startswith("_")}
        regular_pickup = self._positions["sauce_regular"]
        regular_nozzle = self._positions["_sauce_regular_nozzle"]
        hot_pickup = self._positions["sauce_hot"]
        hot_nozzle = self._positions["_sauce_hot_nozzle"]
        cfg["sauce_regular_nozzle_offset"] = [
            regular_nozzle[0] - regular_pickup[0],
            regular_nozzle[1] - regular_pickup[1],
        ]
        cfg["sauce_hot_nozzle_offset"] = [
            hot_nozzle[0] - hot_pickup[0],
            hot_nozzle[1] - hot_pickup[1],
        ]
        cfg["conveyor_y"] = self._positions["_conveyor"][1]
        cfg["target_x"] = self._positions["_target"][0]
        game_width = cfg["game_bottom_right"][0] - cfg["game_top_left"][0]
        game_height = cfg["game_bottom_right"][1] - cfg["game_top_left"][1]
        cfg.update({
            "click_delay": 0.06, "post_topping_delay": 0.06,
            "fast_click_delay": 0.04, "fast_post_topping_delay": 0.03,
            "scan_interval": 0.05, "post_pizza_cooldown": 0.2,
            "aborted_pizza_cooldown": 0.45,
            "auto_restart": True,
            "restart_action_delay": 1.0, "restart_load_delay": 5.0,
            "restart_done_rel": [0.149, 0.944],
            "restart_reward_close_rel": [0.701, 0.134],
            "restart_kitchen_rel": [0.264, 0.248],
            "restart_confirm_yes_rel": [0.428, 0.459],
            "restart_play_rel": [0.160, 0.812],
            "target_zone_radius": 130, "pizza_edge_margin": 12,
            "pizza_radius_x": round(game_width * 0.165),
            "pizza_radius_y": round(game_height * 0.173),
            "pizza_tracking_cache_max_age": 6.0,
            "fast_conveyor_speed": 130.0, "turbo_conveyor_speed": 320.0,
            "topping_drop_lead_seconds": 0.10,
            "topping_right_margin_factor": 0.42,
            "order_samples": 3, "order_sample_interval": 0.025,
            "order_min_confidence": 0.5, "order_retry_window": 0.45,
            "motion_sample_interval": 0.04, "motion_sample_count": 5,
            "conveyor_speed_default": 90.0,
            "sauce_motion_step_delay": 0.029,
            "fast_sauce_motion_step_delay": 0.017,
            "sauce_pickup_delay": 0.075, "fast_sauce_pickup_delay": 0.05,
            "sauce_fill_x_factor": 0.84, "sauce_fill_y_factor": 0.73,
            "sauce_edge_laps": 0,
            "sauce_fill_points": 64, "fast_sauce_fill_points": 40,
            "minimum_sauce_fill_points": 24, "sauce_edge_points": 20,
            "sauce_reanchor_interval": 0.40,
            "fast_sauce_reanchor_interval": 0.22,
            "sauce_center_offset_x": 3, "sauce_center_offset_y": -6,
            "sauce_right_margin_factor": 0.84,
            "estimated_drop_seconds": 0.30,
            "fast_estimated_drop_seconds": 0.24,
            "fast_action_reserve": 0.18,
            "sauce_coverage_warn_threshold": 0.55,
            "dough_hsv_lower": [10, 20, 155], "dough_hsv_upper": [40, 170, 255],
            "pizza_min_area": 600, "conveyor_strip_half_height": 75,
        })
        save_paths = {CONFIG_FILE, _appdir("config.json"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")}
        for sp in save_paths:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(sp)), exist_ok=True)
                with open(sp, "w") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass
        self._on_done()
        self.destroy()


# ═════════════════════════════════════════════════════════════════
#  Main GUI
# ═════════════════════════════════════════════════════════════════
class PizzaBotGUI:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"Pizzatron 3000 Bot — {DISCORD_HANDLE}")
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.resizable(False, False)
        self.root.configure(bg="#0e0e18")

        self.msg_queue: queue.Queue[str] = queue.Queue()
        self.bot: Optional[PizzaBot] = None
        self.bot_thread: Optional[threading.Thread] = None

        # ── Background image ────────────────────────────────────
        self.bg_photo: Optional[ImageTk.PhotoImage] = None
        self._load_bg()

        self.canvas = tk.Canvas(self.root, width=WIN_W, height=WIN_H,
                                highlightthickness=0, bg="#0e0e18")
        self.canvas.pack(fill="both", expand=True)

        if self.bg_photo:
            self.canvas.create_image(0, 0, anchor="nw", image=self.bg_photo)

        # ── Title on image ───────────────────────────────────────
        # Shadow
        self.canvas.create_text(WIN_W // 2 + 2, 38,
                                text="PIZZATRON 3000 BOT",
                                font=("Segoe UI", 20, "bold"), fill="#2a1a0a")
        # Text
        self.canvas.create_text(WIN_W // 2, 36,
                                text="PIZZATRON 3000 BOT",
                                font=("Segoe UI", 20, "bold"), fill="white")
        self.canvas.create_text(WIN_W - 28, WIN_H - 12,
                                text=f"Discord: {DISCORD_HANDLE}",
                                font=("Segoe UI", 8), fill=FG_DIM,
                                anchor="se")

        # ── Build cards on top ───────────────────────────────────
        self._build_dashboard()
        self._build_controls()
        self._build_log()
        self._build_toolbar()

        self._poll_id = self.root.after(60, self._poll)

        if not os.path.exists(CONFIG_FILE):
            self._append_log("⚠  No config.json — click Calibrate first.")

    # ── Background loading ───────────────────────────────────────

    def _load_bg(self):
        if not os.path.exists(BG_IMAGE):
            return
        try:
            img = Image.open(BG_IMAGE).convert("RGB")
            # Resize-to-cover + center-crop
            ir = img.width / img.height
            wr = WIN_W / WIN_H
            if ir > wr:
                nh = WIN_H
                nw = int(WIN_H * ir)
            else:
                nw = WIN_W
                nh = int(WIN_W / ir)
            img = img.resize((nw, nh), Image.Resampling.LANCZOS)
            left = (nw - WIN_W) // 2
            top = (nh - WIN_H) // 2
            img = img.crop((left, top, left + WIN_W, top + WIN_H))
            # Darken for readability
            img = ImageEnhance.Brightness(img).enhance(0.38)
            self.bg_photo = ImageTk.PhotoImage(img)
        except Exception:
            self.bg_photo = None

    # ── Dashboard card ───────────────────────────────────────────

    def _build_dashboard(self):
        card = tk.Frame(self.root, bg=CARD, highlightbackground=CARD_LIGHT,
                        highlightthickness=1)
        card.place(x=28, y=68, width=WIN_W - 56, height=96)

        # Row 1: status + pizza count
        r1 = tk.Frame(card, bg=CARD)
        r1.pack(fill="x", padx=18, pady=(14, 2))

        tk.Label(r1, text="STATUS", font=("Segoe UI", 8), bg=CARD,
                 fg=FG_DIM).pack(side="left")
        self._lbl_status = tk.Label(r1, text="●  Idle",
                                    font=("Segoe UI", 12, "bold"),
                                    bg=CARD, fg=FG_DIM)
        self._lbl_status.pack(side="left", padx=(8, 0))

        self._lbl_pizzas = tk.Label(r1, text="0",
                                    font=("Segoe UI", 22, "bold"),
                                    bg=CARD, fg=ACCENT)
        self._lbl_pizzas.pack(side="right")
        tk.Label(r1, text="PIZZAS", font=("Segoe UI", 8), bg=CARD,
                 fg=FG_DIM).pack(side="right", padx=(0, 8))

        # Row 2: current order
        r2 = tk.Frame(card, bg=CARD)
        r2.pack(fill="x", padx=18, pady=(0, 12))

        tk.Label(r2, text="ORDER", font=("Segoe UI", 8), bg=CARD,
                 fg=FG_DIM).pack(side="left")
        self._lbl_order = tk.Label(r2, text="—", font=("Segoe UI", 10),
                                   bg=CARD, fg=YELLOW)
        self._lbl_order.pack(side="left", padx=(8, 0))

    # ── Control buttons ──────────────────────────────────────────

    def _build_controls(self):
        card = tk.Frame(self.root, bg=CARD, highlightbackground=CARD_LIGHT,
                        highlightthickness=1)
        card.place(x=28, y=172, width=WIN_W - 56, height=54)

        inner = tk.Frame(card, bg=CARD)
        inner.pack(expand=True)

        self._btn_start = tk.Button(
            inner, text="▶  Start", font=("Segoe UI", 11, "bold"),
            bg="#40a02b", fg="white", activebackground="#6bc550",
            relief="flat", width=10, pady=4, command=self._on_start)
        self._btn_start.pack(side="left", padx=4)

        self._btn_pause = tk.Button(
            inner, text="⏸  Pause", font=("Segoe UI", 11, "bold"),
            bg="#df8e1d", fg="white", activebackground="#e6a73a",
            relief="flat", width=10, pady=4, state="disabled",
            command=self._on_pause)
        self._btn_pause.pack(side="left", padx=4)

        self._btn_stop = tk.Button(
            inner, text="⏹  Stop", font=("Segoe UI", 11, "bold"),
            bg="#d20f39", fg="white", activebackground="#e64569",
            relief="flat", width=10, pady=4, state="disabled",
            command=self._on_stop)
        self._btn_stop.pack(side="left", padx=4)

    # ── Activity log ─────────────────────────────────────────────

    def _build_log(self):
        card = tk.Frame(self.root, bg=CARD, highlightbackground=CARD_LIGHT,
                        highlightthickness=1)
        card.place(x=28, y=234, width=WIN_W - 56, height=350)

        tk.Label(card, text="ACTIVITY LOG", font=("Segoe UI", 8),
                 bg=CARD, fg=FG_DIM, anchor="w").pack(fill="x", padx=14,
                                                       pady=(10, 2))

        self._log_text = scrolledtext.ScrolledText(
            card, font=("Consolas", 9), bg="#12121e", fg=FG,
            insertbackground=FG, relief="flat", wrap="word",
            state="disabled", borderwidth=0, highlightthickness=0)
        self._log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # ── Bottom toolbar ───────────────────────────────────────────

    def _build_toolbar(self):
        card = tk.Frame(self.root, bg=CARD, highlightbackground=CARD_LIGHT,
                        highlightthickness=1)
        card.place(x=28, y=594, width=WIN_W - 56, height=48)

        inner = tk.Frame(card, bg=CARD)
        inner.pack(expand=True)

        self._btn_calib = tk.Button(
            inner, text="🔧 Calibrate", font=("Segoe UI", 9),
            bg=CARD_LIGHT, fg=FG, activebackground="#3a3a56",
            relief="flat", padx=10, pady=3, command=self._on_calibrate)
        self._btn_calib.pack(side="left", padx=4)

        self._btn_test = tk.Button(
            inner, text="🧪 Test", font=("Segoe UI", 9),
            bg=CARD_LIGHT, fg=FG, activebackground="#3a3a56",
            relief="flat", padx=10, pady=3, command=self._on_test)
        self._btn_test.pack(side="left", padx=4)

        tk.Button(
            inner, text="📂 Config", font=("Segoe UI", 9),
            bg=CARD_LIGHT, fg=FG, activebackground="#3a3a56",
            relief="flat", padx=10, pady=3,
            command=self._open_config).pack(side="left", padx=4)

    # ── Log helper ───────────────────────────────────────────────

    def _append_log(self, msg: str):
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg.rstrip() + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _bot_log(self, msg: str):
        self.msg_queue.put(msg)

    # ── Poll (drain queue + update dashboard) ────────────────────

    def _poll(self):
        while True:
            try:
                self._append_log(self.msg_queue.get_nowait())
            except queue.Empty:
                break

        if self.bot is not None:
            st = self.bot.status
            colour = {"Idle": FG_DIM, "Running": GREEN,
                      "Restarting": BLUE, "Paused": YELLOW,
                      "Stopped": RED}.get(st, FG_DIM)
            self._lbl_status.config(text=f"●  {st}", fg=colour)
            self._lbl_pizzas.config(text=str(self.bot.pizzas_made))
            self._lbl_order.config(text=self.bot.current_order_str or "—")

            if not self.bot.running:
                self._btn_start.config(state="normal")
                self._btn_pause.config(state="disabled")
                self._btn_stop.config(state="disabled")
                self._btn_calib.config(state="normal")
                self._btn_test.config(state="normal")

        self._poll_id = self.root.after(60, self._poll)

    # ── Button handlers ──────────────────────────────────────────

    def _ensure_config(self) -> bool:
        if not os.path.exists(CONFIG_FILE):
            messagebox.showwarning("No Calibration",
                                   "config.json not found.\nClick Calibrate first.")
            return False
        return True

    def _on_start(self):
        if not self._ensure_config():
            return

        # Resume if paused
        if self.bot and self.bot.running and self.bot.paused:
            self.bot.paused = False
            self.bot.status = "Running"
            self._btn_start.config(state="disabled")
            self._btn_pause.config(state="normal")
            self._btn_stop.config(state="normal")
            self._append_log("▶  Resumed")
            return

        # Fresh start
        self._append_log("—" * 40)
        self.bot = PizzaBot(config_path=CONFIG_FILE, log_fn=self._bot_log)
        self.bot.paused = False
        self.bot.status = "Running"

        self.bot_thread = threading.Thread(
            target=self.bot.run, kwargs={"setup_keyboard": False}, daemon=True)
        self.bot_thread.start()

        self._btn_start.config(state="disabled")
        self._btn_pause.config(state="normal")
        self._btn_stop.config(state="normal")
        self._btn_calib.config(state="disabled")
        self._btn_test.config(state="disabled")

    def _on_pause(self):
        if self.bot and self.bot.running:
            self.bot.paused = True
            self.bot.status = "Paused"
            self._btn_start.config(state="normal")
            self._btn_pause.config(state="disabled")
            self._append_log("⏸  Paused — click Start to resume")

    def _on_stop(self):
        if self.bot:
            self.bot.running = False
            self.bot.status = "Stopped"
            self._append_log("⏹  Stopped")
        self._btn_start.config(state="normal")
        self._btn_pause.config(state="disabled")
        self._btn_stop.config(state="disabled")
        self._btn_calib.config(state="normal")
        self._btn_test.config(state="normal")

    def _on_calibrate(self):
        CalibrationWizard(self.root, on_done=self._on_calib_done)

    def _on_calib_done(self):
        self._append_log("✅  Calibration saved!")

    def _on_test(self):
        if not self._ensure_config():
            return
        self._append_log("—" * 40)
        self._append_log("🧪  Multi-frame detection test…")
        try:
            bot = PizzaBot(config_path=CONFIG_FILE, log_fn=self._bot_log)
            game_img = bot.grab_game()
            px = bot.detect_pizza_x(game_img)
            if px is not None:
                self._append_log(f"  🍕  Pizza found at x={px}")
            else:
                self._append_log("  ❌  No pizza on conveyor")
            order = bot.detect_order()
            self._append_log(f"  📋  Order: {order.description}")
            self._append_log("🧪  Done")
        except OrderDetectionError as e:
            self._append_log(f"  ⚠  Order board unreadable: {e}")
            self._append_log("🧪  Done — no action taken")
        except Exception as e:
            self._append_log(f"  ⚠  Error: {e}")

    def _open_config(self):
        if os.path.exists(CONFIG_FILE):
            os.startfile(CONFIG_FILE)
        else:
            messagebox.showinfo("Config", "config.json not found.\nRun Calibrate first.")

    # ── Run ──────────────────────────────────────────────────────

    def run(self):
        self._append_log("🍕  Pizzatron 3000 Bot ready")
        self._append_log(f"   Discord: {DISCORD_HANDLE}")
        self._append_log("   Click Start to play, or Calibrate to set up.\n")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        self.root.lift()
        self.root.focus_force()

        self.root.mainloop()

    def _on_close(self):
        if self.bot and self.bot.running:
            self.bot.running = False
            time.sleep(0.2)
        self.root.destroy()


# ═════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = PizzaBotGUI()
    app.run()
