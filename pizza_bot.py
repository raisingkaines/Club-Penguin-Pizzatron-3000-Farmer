"""
Pizzatron 3000 Bot — Automated Pizza Maker
============================================
Uses fast screen capture (mss) + colour detection (OpenCV) to read orders
and apply the correct toppings via mouse clicks (PyAutoGUI).

Controls
--------
  F2   Start / Pause the bot
  ESC  Emergency stop — quit immediately

Run modes
---------
  python pizza_bot.py              # Normal (plays the game)
  python pizza_bot.py --test       # Detection only (no mouse clicks)
  python pizza_bot.py --debug      # Saves annotated frames to debug_frames/
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from statistics import median
from typing import Optional

import cv2
import keyboard
import mss
import numpy as np
import pyautogui

# ── PyAutoGUI settings ──────────────────────────────────────────
pyautogui.PAUSE = 0.01          # minimal internal pause
pyautogui.FAILSAFE = True       # move mouse to top-left corner → abort

CONFIG_FILE = "config.json"
DEBUG_DIR = "debug_frames"


# ═══════════════════════════════════════════════════════════════
#  Topping enum — values match the keys stored in config.json
# ═══════════════════════════════════════════════════════════════
class Topping(Enum):
    SAUCE       = "sauce_regular"
    HOT_SAUCE   = "sauce_hot"
    CHEESE      = "topping_cheese"
    SEAWEED     = "topping_seaweed"
    SHRIMP      = "topping_shrimp"
    SQUID       = "topping_squid"
    FISH        = "topping_fish"


EXTRA_TOPPINGS = (
    Topping.SEAWEED,
    Topping.SHRIMP,
    Topping.SQUID,
    Topping.FISH,
)


@dataclass(frozen=True)
class PizzaGeometry:
    """Screen-space position and calibrated size of a conveyor pizza."""

    center_x: int
    center_y: int
    radius_x: int
    radius_y: int
    bbox: tuple[int, int, int, int]
    fully_visible: bool


@dataclass
class DetectedOrder:
    """A recognized order, including the exact number of each extra piece."""

    sauce: Topping
    extras: dict[Topping, int] = field(default_factory=dict)
    confidence: float = 1.0
    line_widths: tuple[float, ...] = ()

    def __iter__(self):
        # Preserve the old iterable interface used by the GUI and small scripts.
        yield self.sauce
        yield Topping.CHEESE
        yield from self.extras

    @property
    def signature(self) -> tuple:
        return (
            self.sauce.value,
            tuple((t.value, self.extras[t]) for t in EXTRA_TOPPINGS if t in self.extras),
        )

    @property
    def description(self) -> str:
        parts = [self.sauce.name, Topping.CHEESE.name]
        parts.extend(f"{t.name} x{self.extras[t]}" for t in EXTRA_TOPPINGS if t in self.extras)
        return " + ".join(parts)


class OrderDetectionError(RuntimeError):
    """Raised when the order board cannot be read confidently."""


class PizzaTrackingError(RuntimeError):
    """Raised when the active pizza can no longer be targeted safely."""


# ═══════════════════════════════════════════════════════════════
#  PizzaBot
# ═══════════════════════════════════════════════════════════════
class PizzaBot:
    """Core bot: capture → detect → click → repeat."""

    def __init__(
        self,
        config_path: str = CONFIG_FILE,
        debug: bool = False,
        test_mode: bool = False,
        log_fn=None,
    ):
        self.cfg = self._load_config(config_path)
        self.sct = mss.mss()
        self.debug = debug
        self.test_mode = test_mode
        self._log_fn = log_fn or print

        self.running = False
        self.paused = True
        self.pizzas_made = 0
        self.frame_idx = 0

        # Observable state for GUI / callers
        self.status = "Idle"
        self.current_order_str = ""

        # End-of-game navigation state.  Click targets are normalized to the
        # captured game area, so moving the browser after calibration does not
        # turn them into hard-coded desktop coordinates.
        self._restart_next_action_at = 0.0
        self._restart_guard_until = 0.0
        self._restart_last_state: Optional[str] = None
        self._restart_candidate: Optional[str] = None
        self._restart_candidate_frames = 0

        if debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)

    def log(self, msg: str) -> None:
        """Route a message to the configured log sink."""
        self._log_fn(msg)

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _load_config(path: str) -> dict:
        candidates = [
            path,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"),
            os.path.join(os.getcwd(), "config.json"),
            os.path.join(os.getcwd(), "dist", "config.json"),
        ]
        found_path = None
        for c in candidates:
            if os.path.exists(c):
                found_path = c
                break

        if not found_path:
            print(f"ERROR  '{path}' not found — run calibration first.")
            sys.exit(1)
        with open(found_path) as fh:
            return json.load(fh)

    def _grab(self, x1: int, y1: int, x2: int, y2: int) -> np.ndarray:
        """Grab a screen rectangle and return a BGR numpy array."""
        mon = {"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1}
        return cv2.cvtColor(np.array(self.sct.grab(mon)), cv2.COLOR_BGRA2BGR)

    def grab_game(self) -> np.ndarray:
        tl, br = self.cfg["game_top_left"], self.cfg["game_bottom_right"]
        return self._grab(tl[0], tl[1], br[0], br[1])

    def grab_order_board(self) -> np.ndarray:
        tl, br = self.cfg["order_top_left"], self.cfg["order_bottom_right"]
        return self._grab(tl[0], tl[1], br[0], br[1])

    # ── end-of-game navigation ──────────────────────────────────

    @staticmethod
    def _roi_hsv_fraction(
        image: np.ndarray,
        bounds: tuple[float, float, float, float],
        lower: tuple[int, int, int],
        upper: tuple[int, int, int],
    ) -> float:
        """Return the fraction of an image-relative ROI inside an HSV range."""
        if image is None or image.size == 0 or image.ndim != 3:
            return 0.0

        height, width = image.shape[:2]
        left, top, right, bottom = bounds
        x1 = max(0, min(width, round(left * width)))
        y1 = max(0, min(height, round(top * height)))
        x2 = max(x1 + 1, min(width, round(right * width)))
        y2 = max(y1 + 1, min(height, round(bottom * height)))
        roi = image[y1:y2, x1:x2]
        if roi.size == 0:
            return 0.0

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array(lower, dtype=np.uint8),
            np.array(upper, dtype=np.uint8),
        )
        return cv2.countNonZero(mask) / max(1, mask.shape[0] * mask.shape[1])

    @staticmethod
    def _detect_restart_screen(game_img: np.ndarray) -> Optional[str]:
        """Recognize every screen between a finished round and gameplay.

        The checks combine large, distinctive colour regions instead of
        trusting a single pixel or button.  Reward must be tested first
        because its modal sits on top of the still-visible score screen.  The
        play-confirmation modal must precede the parlor check because the room
        remains visible behind it.
        """
        blue_modal = PizzaBot._roi_hsv_fraction(
            game_img, (0.27, 0.07, 0.75, 0.88), (85, 80, 50), (125, 255, 255)
        )
        if blue_modal >= 0.65:
            return "reward"

        confirmation_blue = PizzaBot._roi_hsv_fraction(
            game_img, (0.28, 0.25, 0.72, 0.53), (85, 80, 50), (125, 255, 255)
        )
        confirmation_below = PizzaBot._roi_hsv_fraction(
            game_img, (0.30, 0.62, 0.70, 0.78), (85, 80, 50), (125, 255, 255)
        )
        if confirmation_blue >= 0.85 and confirmation_below <= 0.35:
            return "confirm"

        done_orange = PizzaBot._roi_hsv_fraction(
            game_img, (0.05, 0.86, 0.25, 0.995), (5, 80, 60), (30, 255, 255)
        )
        done_white = PizzaBot._roi_hsv_fraction(
            game_img, (0.05, 0.86, 0.25, 0.995), (0, 0, 200), (180, 45, 255)
        )
        score_panel = PizzaBot._roi_hsv_fraction(
            game_img, (0.0, 0.0, 0.36, 0.75), (0, 0, 120), (180, 100, 255)
        )
        if done_orange >= 0.65 and done_white >= 0.04 and score_panel >= 0.30:
            return "score"

        start_play_white = PizzaBot._roi_hsv_fraction(
            game_img, (0.02, 0.76, 0.30, 0.86), (0, 0, 180), (180, 70, 255)
        )
        start_logo_red = PizzaBot._roi_hsv_fraction(
            game_img, (0.05, 0.22, 0.42, 0.46), (0, 100, 70), (10, 255, 255)
        )
        if start_play_white >= 0.45 and start_logo_red >= 0.20:
            return "start"

        parlor_green = PizzaBot._roi_hsv_fraction(
            game_img, (0.28, 0.0, 0.48, 0.48), (30, 70, 35), (90, 255, 255)
        )
        parlor_bar = PizzaBot._roi_hsv_fraction(
            game_img, (0.12, 0.91, 0.90, 0.995), (85, 80, 50), (125, 255, 255)
        )
        if parlor_green >= 0.30 and parlor_bar >= 0.50:
            return "parlor"

        return None

    def _click_game_relative(
        self,
        point: list[float] | tuple[float, float],
        game_img: Optional[np.ndarray] = None,
    ) -> None:
        """Click a normalized point inside the calibrated game rectangle."""
        top_left = self.cfg["game_top_left"]
        if game_img is not None and game_img.size:
            height, width = game_img.shape[:2]
        else:
            bottom_right = self.cfg["game_bottom_right"]
            width = max(1, int(bottom_right[0]) - int(top_left[0]))
            height = max(1, int(bottom_right[1]) - int(top_left[1]))
        x = round(int(top_left[0]) + float(point[0]) * width)
        y = round(int(top_left[1]) + float(point[1]) * height)
        pyautogui.moveTo(x, y, duration=0.08)
        time.sleep(0.04)
        pyautogui.click()

    def _handle_restart_flow(self, game_img: np.ndarray, now: float) -> bool:
        """Advance end-screen navigation and suppress pizza actions meanwhile."""
        state = self._detect_restart_screen(game_img)
        guard_until = float(getattr(self, "_restart_guard_until", 0.0))
        if state is None:
            self._restart_candidate = None
            self._restart_candidate_frames = 0
            if now < guard_until:
                return True
            if getattr(self, "_restart_last_state", None) is not None:
                self._restart_last_state = None
                self.status = "Running"
            return False

        # Two matching frames prevent a transient animation frame from
        # causing a click, while still reacting in roughly 160 ms.
        if state == getattr(self, "_restart_candidate", None):
            self._restart_candidate_frames += 1
        else:
            self._restart_candidate = state
            self._restart_candidate_frames = 1

        self.status = "Restarting"
        self.current_order_str = ""
        if (
            self._restart_candidate_frames < 2
            or now < float(getattr(self, "_restart_next_action_at", 0.0))
        ):
            return True

        actions = {
            "score": (
                "End screen detected — clicking Done",
                self.cfg.get("restart_done_rel", [0.149, 0.944]),
            ),
            "reward": (
                "Reward popup detected — closing it",
                self.cfg.get("restart_reward_close_rel", [0.701, 0.134]),
            ),
            "parlor": (
                "Pizza Parlor detected — re-entering the Kitchen",
                self.cfg.get("restart_kitchen_rel", [0.264, 0.248]),
            ),
            "confirm": (
                "Play confirmation detected — clicking Yes",
                self.cfg.get("restart_confirm_yes_rel", [0.428, 0.459]),
            ),
            "start": (
                "Pizzatron start screen detected — clicking Play",
                self.cfg.get("restart_play_rel", [0.160, 0.812]),
            ),
        }
        message, point = actions[state]
        if self.test_mode:
            self.log(f"  TEST: Would click — {message}")
        else:
            icon = {
                "score": "🏁",
                "reward": "🪙",
                "parlor": "🚪",
                "confirm": "✅",
                "start": "▶️",
            }[state]
            self.log(f"  {icon}  {message}")
            self._click_game_relative(point, game_img)

        # Kitchen opens a confirmation dialog, then Yes opens the Pizzatron
        # start screen.  Only Play starts the expensive gameplay load.
        delay_key = "restart_load_delay" if state == "start" else "restart_action_delay"
        delay_default = 5.0 if state == "start" else 1.0
        delay = max(0.25, float(self.cfg.get(delay_key, delay_default)))
        self._restart_next_action_at = now + delay
        self._restart_guard_until = now + delay
        self._restart_last_state = state
        self._restart_candidate = None
        self._restart_candidate_frames = 0

        # Never let conveyor tracking from the finished round influence the
        # first pizza of the newly loaded round.
        self._last_pizza_geometry = None
        self._last_pizza_geometry_at = None
        self._last_conveyor_speed = float(self.cfg.get("conveyor_speed_default", 90.0))
        return True

    # ── pizza-on-conveyor detection ──────────────────────────────

    def detect_pizza_geometry(self, game_img: np.ndarray) -> Optional[PizzaGeometry]:
        """Find the pizza and report whether the whole dough is on-screen.

        The old detector returned the centroid of a clipped contour.  At the
        left edge that made a mostly off-screen pizza look ready, so sauce was
        applied before the full dough could be reached.  This version also
        checks the detected width and both horizontal edges.
        """
        tl = self.cfg["game_top_left"]
        game_h, game_w = game_img.shape[:2]
        strip_h = int(self.cfg.get("conveyor_strip_half_height", 75))
        conv_y_rel = int(self.cfg["conveyor_y"] - tl[1])

        y1 = max(0, conv_y_rel - strip_h)
        y2 = min(game_h, conv_y_rel + strip_h)
        strip = game_img[y1:y2, :, :]
        if strip.size == 0:
            return None

        hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
        dough_mask = cv2.inRange(
            hsv,
            np.array(self.cfg.get("dough_hsv_lower", [10, 20, 155])),
            np.array(self.cfg.get("dough_hsv_upper", [40, 170, 255])),
        )
        colorful_mask = cv2.inRange(
            hsv, np.array([0, 55, 115]), np.array([180, 255, 255])
        )
        mask = cv2.bitwise_or(dough_mask, colorful_mask)

        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kern)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kern)

        radius_x = int(self.cfg.get("pizza_radius_x", round(game_w * 0.165)))
        radius_y = int(self.cfg.get("pizza_radius_y", round(game_h * 0.173)))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_area = float(self.cfg.get("pizza_min_area", 600))
        min_candidate_width = max(120, int(radius_x * 1.20))
        max_candidate_width = int(radius_x * 2.70)
        min_candidate_height = max(35, int((y2 - y1) * 0.42))
        candidates = []
        for contour in contours:
            area = cv2.contourArea(contour)
            _bx, _by, width, height = cv2.boundingRect(contour)
            aspect = width / max(1, height)
            if (
                area >= min_area
                and min_candidate_width <= width <= max_candidate_width
                and height >= min_candidate_height
                and 1.15 <= aspect <= 4.8
            ):
                candidates.append(contour)
        if not candidates:
            return None

        # Prefer the active track's predicted X during ingredient placement;
        # otherwise use the contour closest to the calibrated footprint.
        # This prevents a clean incoming pizza from stealing the track while
        # the older, topped pizza is still moving toward the right edge.
        expected_width = radius_x * 2.0
        expected_height = min(float(y2 - y1), radius_y * 2.0)
        preferred_x = getattr(self, "_pizza_detection_preferred_x", None)

        def candidate_key(contour) -> tuple[float, float]:
            candidate_x, _candidate_y, candidate_w, candidate_h = cv2.boundingRect(contour)
            shape_cost = (
                abs(candidate_w - expected_width) / expected_width
                + abs(candidate_h - expected_height) / expected_height
            )
            if preferred_x is None:
                return shape_cost, 0.0
            center_x_abs = tl[0] + candidate_x + candidate_w / 2.0
            track_distance = abs(center_x_abs - float(preferred_x)) / max(1.0, radius_x)
            return track_distance, shape_cost

        best = min(candidates, key=candidate_key)
        bx, by, bw, bh = cv2.boundingRect(best)

        # The uploaded game frame measures the crust at about 33% x 35% of
        # the game dimensions.  Values remain configurable for other servers.
        center_x_rel = bx + bw // 2
        center_x = center_x_rel + tl[0]
        center_y = int(self.cfg["conveyor_y"])

        edge_margin = max(4, int(game_w * 0.005))
        min_full_width = max(120, int(radius_x * 1.35))
        fully_visible = (
            bx > edge_margin
            and bx + bw < game_w - edge_margin
            and bw >= min_full_width
        )

        if self.debug and self.frame_idx % 5 == 0:
            dbg = strip.copy()
            colour = (0, 255, 0) if fully_visible else (0, 165, 255)
            cv2.drawContours(dbg, [best], -1, colour, 2)
            cv2.rectangle(dbg, (bx, by), (bx + bw, by + bh), colour, 2)
            debug_center_y = max(0, min(dbg.shape[0] - 1, conv_y_rel - y1))
            cv2.circle(dbg, (center_x_rel, debug_center_y), 5, (0, 0, 255), -1)
            cv2.imwrite(f"{DEBUG_DIR}/conv_{self.frame_idx:05d}.png", dbg)

        return PizzaGeometry(
            center_x=center_x,
            center_y=center_y,
            radius_x=radius_x,
            radius_y=radius_y,
            bbox=(bx + tl[0], by + y1 + tl[1], bw, bh),
            fully_visible=fully_visible,
        )

    def detect_pizza_x(self, game_img: np.ndarray) -> Optional[int]:
        """Backward-compatible X-only wrapper used by the GUI and callers."""
        geometry = self.detect_pizza_geometry(game_img)
        return geometry.center_x if geometry is not None else None

    # ── order detection ──────────────────────────────────────────

    @staticmethod
    def _extract_order_lines(board: np.ndarray) -> tuple[np.ndarray, list[float]]:
        """Return the ingredient text mask and scale-normalized line widths."""
        h, w = board.shape[:2]
        scale = max(0.1, w / 407.0)

        # Ingredient text is navy blue.  The crop excludes both the pizza
        # illustration and the right-hand stats column.
        ing_col = board[int(h * 0.14):int(h * 0.88), int(w * 0.42):int(w * 0.76)]
        b = ing_col[:, :, 0].astype(np.int16)
        g = ing_col[:, :, 1].astype(np.int16)
        r = ing_col[:, :, 2].astype(np.int16)
        text_mask = (
            (b > 60)
            & (b < 225)
            & (b > r + 18)
            & (b > g + 8)
            & (r < 135)
        ).astype(np.uint8)

        row_threshold = max(2, int(round(2.5 * scale)))
        active_rows = np.flatnonzero(text_mask.sum(axis=1) >= row_threshold)
        if active_rows.size == 0:
            return text_mask, []

        groups: list[tuple[int, int]] = []
        start = prev = int(active_rows[0])
        max_gap = max(2, int(round(1.5 * scale)))
        for y_value in active_rows[1:]:
            y = int(y_value)
            if y - prev > max_gap:
                groups.append((start, prev + 1))
                start = y
            prev = y
        groups.append((start, prev + 1))

        widths: list[float] = []
        min_height = max(4, int(round(5 * scale)))
        for sy, ey in groups:
            if ey - sy < min_height:
                continue
            points = cv2.findNonZero(text_mask[sy:ey, :])
            if points is None:
                continue
            _x, _y, line_width, _height = cv2.boundingRect(points)
            widths.append(line_width / scale)

        return text_mask, widths

    @staticmethod
    def _preview_topping_scores(board: np.ndarray) -> dict[Topping, float]:
        """Measure independent colour evidence in the board's pizza preview."""
        h, w = board.shape[:2]
        preview = board[int(h * 0.12):int(h * 0.75), int(w * 0.02):int(w * 0.44)]
        if preview.size == 0:
            return {t: 0.0 for t in EXTRA_TOPPINGS}
        hsv = cv2.cvtColor(preview, cv2.COLOR_BGR2HSV)
        total = max(1, preview.shape[0] * preview.shape[1])

        masks = {
            Topping.SEAWEED: cv2.inRange(
                hsv, np.array([35, 50, 40]), np.array([85, 255, 255])
            ),
            Topping.SQUID: cv2.inRange(
                hsv, np.array([88, 55, 40]), np.array([138, 255, 255])
            ),
            Topping.FISH: cv2.inRange(
                hsv, np.array([0, 0, 65]), np.array([180, 38, 200])
            ),
            # Shrimp bodies are pale pink (low saturation, very bright).
            # The former peach/red mask also matched the crust and sauce on
            # every pizza, so it could invent shrimp on unrelated orders.
            Topping.SHRIMP: cv2.inRange(
                hsv, np.array([0, 16, 205]), np.array([14, 110, 255])
            ),
        }
        return {t: cv2.countNonZero(mask) / total for t, mask in masks.items()}

    @staticmethod
    def _classify_order_lines(
        widths: list[float],
        preview_scores: Optional[dict[Topping, float]] = None,
    ) -> DetectedOrder:
        """Classify ordered ingredient rows using relative, not absolute, widths.

        The first two rows are always CHEESE and the sauce.  Comparing the
        second row to CHEESE is stable across resolution and fixes the old
        boundary where a real 68 px HOT SAUCE row was called PIZZA SAUCE.
        Extra-row profiles support both servers that show just the name and
        servers that prefix it with a quantity (for example ``5 SHRIMP``).
        """
        if len(widths) < 2:
            raise OrderDetectionError(f"expected at least 2 ingredient rows, found {len(widths)}")
        if len(widths) > 6:
            raise OrderDetectionError(f"too many ingredient rows: {len(widths)}")

        cheese_width = max(1.0, widths[0])
        sauce_ratio = widths[1] / cheese_width
        # Two HOT SAUCE profiles cover the compact text used by the classic
        # client and the wider Burbank rendering seen in modern recreations.
        expected_sauce = {
            Topping.SAUCE: (1.68,),
            Topping.HOT_SAUCE: (1.23, 1.45),
        }
        if not 1.12 <= sauce_ratio <= 1.90:
            raise OrderDetectionError(f"unrecognized sauce row ratio {sauce_ratio:.2f}")
        # Leave a deliberate dead-band instead of silently turning an
        # uncertain HOT SAUCE line into regular sauce.  Three-frame voting can
        # recover from a single poor capture; an actually ambiguous board is
        # skipped safely.
        if sauce_ratio <= 1.52:
            sauce = Topping.HOT_SAUCE
        elif sauce_ratio >= 1.58:
            sauce = Topping.SAUCE
        else:
            raise OrderDetectionError(f"ambiguous sauce row ratio {sauce_ratio:.2f}")
        sauce_distances = {
            topping: min(abs(sauce_ratio - expected) for expected in expectations)
            for topping, expectations in expected_sauce.items()
        }

        extra_ratios = [width / cheese_width for width in widths[2:]]
        extra_count = len(extra_ratios)
        if extra_count == 0:
            confidence = max(0.55, 1.0 - sauce_distances[sauce] * 2.5)
            return DetectedOrder(sauce, {}, confidence, tuple(widths))
        if extra_count not in (1, 2, 4):
            raise OrderDetectionError(
                f"unsupported extra ingredient count: {extra_count}"
            )

        # Widths measured from the game's Burbank font.  The second profile
        # includes a leading amount (1/2/5), whose glyph width is constant.
        profiles = (
            {
                Topping.SEAWEED: 1.11,
                Topping.SHRIMP: 0.96,
                Topping.SQUID: 0.81,
                Topping.FISH: 0.64,
            },
            {
                Topping.SEAWEED: 1.43,
                Topping.SHRIMP: 1.26,
                Topping.SQUID: 1.04,
                Topping.FISH: 0.81,
            },
        )
        preview_scores = preview_scores or {t: 0.0 for t in EXTRA_TOPPINGS}
        thresholds = {
            Topping.SEAWEED: 0.004,
            Topping.SHRIMP: 0.0015,
            Topping.SQUID: 0.004,
            Topping.FISH: 0.010,
        }
        # Keep sub-threshold colour information instead of collapsing it to a
        # boolean.  On a quantity-prefixed order, ``5 SQUID`` can have almost
        # the same measured width as a bare ``SEAWEED`` row.  A slightly
        # clipped blue preview used to fall just below 0.004, lose all weight,
        # and let that width overlap silently choose seaweed.
        colour_strength = {
            topping: min(
                2.0,
                max(0.0, float(preview_scores.get(topping, 0.0)))
                / thresholds[topping],
            )
            for topping in EXTRA_TOPPINGS
        }

        # When the preview shows exactly as many distinct topping colours as
        # there are extra rows, it is stronger evidence than text width.  The
        # live client can render ``5 SQUID`` at roughly 1.66x CHEESE width,
        # far outside the nominal font profile, while its blue squid preview
        # remains unmistakable.  Use that evidence only when the selected
        # colours have a clear margin over every unselected colour.
        ranked_colours = sorted(
            ((strength, topping) for topping, strength in colour_strength.items()),
            reverse=True,
            key=lambda item: item[0],
        )
        selected_colours = ranked_colours[:extra_count]
        weakest_selected = min(strength for strength, _topping in selected_colours)
        strongest_unselected = (
            ranked_colours[extra_count][0]
            if extra_count < len(ranked_colours)
            else 0.0
        )
        if (
            weakest_selected >= 0.75
            and weakest_selected - strongest_unselected >= 0.30
        ):
            selected_set = {topping for _strength, topping in selected_colours}
            pieces_each = {1: 5, 2: 2, 4: 1}[extra_count]
            extras = {
                topping: pieces_each
                for topping in EXTRA_TOPPINGS
                if topping in selected_set
            }
            sauce_conf = max(0.0, 1.0 - sauce_distances[sauce] / 0.20)
            visual_conf = min(
                1.0,
                (weakest_selected + weakest_selected - strongest_unselected) / 1.5,
            )
            confidence = max(0.55, min(1.0, 0.50 * sauce_conf + 0.50 * visual_conf))
            return DetectedOrder(sauce, extras, confidence, tuple(widths))

        # The two text profiles overlap specifically around bare SEAWEED and
        # quantity-prefixed SQUID.  Require clear green-vs-blue preview
        # evidence in that band; if the preview is still loading, retrying the
        # order is safer than placing five pieces from the wrong bin.
        if extra_count == 1 and 0.98 <= extra_ratios[0] <= 1.18:
            seaweed_strength = colour_strength[Topping.SEAWEED]
            squid_strength = colour_strength[Topping.SQUID]
            dominant = max(seaweed_strength, squid_strength)
            if dominant < 0.20 or abs(seaweed_strength - squid_strength) < 0.20:
                raise OrderDetectionError(
                    "ambiguous SEAWEED/SQUID row without clear preview colour: "
                    f"ratio={extra_ratios[0]:.2f}"
                )

        candidate_costs: dict[tuple[Topping, ...], float] = {}
        for candidate in combinations(EXTRA_TOPPINGS, extra_count):
            for profile in profiles:
                width_cost = sum(
                    ((ratio - profile[topping]) / 0.11) ** 2
                    for ratio, topping in zip(extra_ratios, candidate)
                )
                colour_cost = 0.0
                for topping in EXTRA_TOPPINGS:
                    strength = colour_strength[topping]
                    if strength > 0:
                        colour_cost += (-0.70 if topping in candidate else 1.05) * strength
                total_cost = width_cost + colour_cost
                candidate_costs[candidate] = min(
                    candidate_costs.get(candidate, float("inf")),
                    total_cost,
                )

        if not candidate_costs:
            raise OrderDetectionError(f"unsupported extra ingredient count: {extra_count}")
        scored = sorted(
            ((cost, candidate) for candidate, cost in candidate_costs.items()),
            key=lambda item: item[0],
        )
        best_cost, best_extras = scored[0]
        second_cost = scored[1][0] if len(scored) > 1 else best_cost + 1.0
        if best_cost > 8.0:
            raise OrderDetectionError(
                f"extra rows do not match known ingredients: ratios={extra_ratios!r}"
            )
        if len(scored) > 1 and second_cost - best_cost < 0.20:
            raise OrderDetectionError(
                f"ambiguous extra ingredient rows: ratios={extra_ratios!r}"
            )

        pieces_each = {1: 5, 2: 2, 4: 1}[extra_count]
        extras = {topping: pieces_each for topping in best_extras}
        sauce_conf = max(0.0, 1.0 - sauce_distances[sauce] / 0.20)
        width_conf = 1.0 / (1.0 + max(0.0, best_cost))
        margin_conf = min(1.0, max(0.0, second_cost - best_cost))
        confidence = max(0.35, min(1.0, 0.45 * sauce_conf + 0.35 * width_conf + 0.20 * margin_conf))
        return DetectedOrder(sauce, extras, confidence, tuple(widths))

    def _parse_order_board(self, board: np.ndarray) -> DetectedOrder:
        text_mask, widths = self._extract_order_lines(board)
        order = self._classify_order_lines(widths, self._preview_topping_scores(board))
        if self.debug:
            cv2.imwrite(f"{DEBUG_DIR}/board_{self.frame_idx:05d}.png", board)
            cv2.imwrite(f"{DEBUG_DIR}/board_text_{self.frame_idx:05d}.png", text_mask * 255)
        return order

    def detect_order(self) -> DetectedOrder:
        """Read several board frames and require a stable recipe."""
        sample_count = max(1, min(5, int(self.cfg.get("order_samples", 3))))
        parsed: list[DetectedOrder] = []
        errors: list[str] = []
        for index in range(sample_count):
            board = self.grab_order_board()
            try:
                parsed.append(self._parse_order_board(board))
            except OrderDetectionError as exc:
                errors.append(str(exc))
            if index + 1 < sample_count:
                time.sleep(float(self.cfg.get("order_sample_interval", 0.025)))

        if not parsed:
            detail = errors[-1] if errors else "no usable board frames"
            raise OrderDetectionError(detail)

        votes = Counter(order.signature for order in parsed)
        signature, vote_count = votes.most_common(1)[0]
        required_votes = sample_count // 2 + 1
        if vote_count < required_votes:
            raise OrderDetectionError(
                f"order was not stable across samples: votes={dict(votes)}, "
                f"valid={len(parsed)}/{sample_count}"
            )

        matching = [order for order in parsed if order.signature == signature]
        winner = max(matching, key=lambda order: order.confidence)
        winner.confidence = sum(order.confidence for order in matching) / len(matching)
        min_confidence = float(self.cfg.get("order_min_confidence", 0.50))
        if winner.confidence < min_confidence:
            raise OrderDetectionError(
                f"order confidence {winner.confidence:.2f} is below {min_confidence:.2f}"
            )
        if self.debug:
            self.log(
                f"  Recognition: widths={tuple(round(v, 1) for v in winner.line_widths)}, "
                f"confidence={winner.confidence:.2f}, votes={vote_count}/{len(parsed)}"
            )
        return winner

    # ── drag and drop actions ─────────────────────────────────────

    def _speed_blend(self, speed: Optional[float] = None) -> float:
        """Return 0 at normal belt speed and 1 at the configured turbo speed."""
        if speed is None:
            speed = float(getattr(
                self,
                "_last_conveyor_speed",
                self.cfg.get("conveyor_speed_default", 90.0),
            ))
        fast_start = float(self.cfg.get("fast_conveyor_speed", 130.0))
        turbo_speed = max(
            fast_start + 1.0,
            float(self.cfg.get("turbo_conveyor_speed", 320.0)),
        )
        return max(0.0, min(1.0, (float(speed) - fast_start) / (turbo_speed - fast_start)))

    def _speed_scaled(self, normal: float, fast: float, speed: Optional[float] = None) -> float:
        blend = self._speed_blend(speed)
        return float(normal) + (float(fast) - float(normal)) * blend

    def _max_safe_drop_x(self, radius_x: int) -> int:
        game_right = int(self.cfg["game_bottom_right"][0])
        margin_factor = float(self.cfg.get("topping_right_margin_factor", 0.42))
        return game_right - max(30, round(radius_x * margin_factor))

    def _drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.10) -> None:
        """Drag from (x1, y1) to (x2, y2) and release."""
        pyautogui.moveTo(x1, y1)
        time.sleep(0.02)
        pyautogui.mouseDown()
        time.sleep(0.03)
        pyautogui.moveTo(x2, y2, duration=duration)
        time.sleep(0.02)
        pyautogui.mouseUp()
        time.sleep(0.03)

    def _detect_pizza_near(
        self, game_img: np.ndarray, preferred_x: float
    ) -> Optional[PizzaGeometry]:
        """Run pizza detection while preferring the active track's predicted X."""
        previous_preference = getattr(self, "_pizza_detection_preferred_x", None)
        self._pizza_detection_preferred_x = float(preferred_x)
        try:
            return self.detect_pizza_geometry(game_img)
        finally:
            if previous_preference is None:
                try:
                    del self._pizza_detection_preferred_x
                except AttributeError:
                    pass
            else:
                self._pizza_detection_preferred_x = previous_preference

    def _fresh_pizza_geometry(self, fallback_x: int) -> PizzaGeometry:
        previous = getattr(self, "_last_pizza_geometry", None)
        previous_at = getattr(self, "_last_pizza_geometry_at", None)
        game_img = self.grab_game()
        now = time.perf_counter()
        cache_max_age = float(self.cfg.get("pizza_tracking_cache_max_age", 6.0))
        preferred_x: Optional[float] = None
        if previous is not None and previous_at is not None:
            elapsed = max(0.0, now - previous_at)
            if elapsed <= cache_max_age:
                speed = float(getattr(
                    self,
                    "_last_conveyor_speed",
                    self.cfg.get("conveyor_speed_default", 90.0),
                ))
                preferred_x = previous.center_x + speed * elapsed
        geometry = (
            self._detect_pizza_near(game_img, preferred_x)
            if preferred_x is not None
            else self.detect_pizza_geometry(game_img)
        )

        # More than one pizza can be visible after a long ingredient pass.
        # Keep subsequent cheese/extras attached to the pizza we started by
        # rejecting a contour that is too far from its predicted position.
        # The main scan explicitly replaces this tracking anchor when it
        # starts a genuinely new order.
        if geometry is not None and previous is not None and previous_at is not None:
            elapsed = max(0.0, now - previous_at)
            if elapsed <= cache_max_age:
                speed = float(getattr(
                    self,
                    "_last_conveyor_speed",
                    self.cfg.get("conveyor_speed_default", 90.0),
                ))
                predicted_x = previous.center_x + speed * elapsed
                identity_radius = max(70.0, previous.radius_x * 0.55)
                if abs(geometry.center_x - predicted_x) > identity_radius:
                    geometry = None

        if geometry is not None:
            if previous is not None and previous_at is not None:
                dt = max(0.001, now - previous_at)
                measured = (geometry.center_x - previous.center_x) / dt
                if 15.0 <= measured <= 650.0:
                    previous_speed = float(getattr(
                        self,
                        "_last_conveyor_speed",
                        self.cfg.get("conveyor_speed_default", 90.0),
                    ))
                    # Short frame intervals amplify a few pixels of contour
                    # jitter.  Smooth those updates so one noisy observation
                    # cannot throw the following topping onto another pizza.
                    self._last_conveyor_speed = 0.65 * previous_speed + 0.35 * measured
            self._last_pizza_geometry = geometry
            self._last_pizza_geometry_at = now
            return geometry
        game_w = self.cfg["game_bottom_right"][0] - self.cfg["game_top_left"][0]
        game_h = self.cfg["game_bottom_right"][1] - self.cfg["game_top_left"][1]
        if previous is not None and previous_at is not None:
            elapsed = max(0.0, now - previous_at)
            speed = float(getattr(
                self,
                "_last_conveyor_speed",
                self.cfg.get("conveyor_speed_default", 90.0),
            ))
            predicted_x = round(previous.center_x + speed * elapsed)
            if elapsed <= cache_max_age:
                return PizzaGeometry(
                    center_x=predicted_x,
                    center_y=previous.center_y,
                    radius_x=previous.radius_x,
                    radius_y=previous.radius_y,
                    bbox=previous.bbox,
                    fully_visible=previous.fully_visible,
                )
        return PizzaGeometry(
            center_x=int(fallback_x),
            center_y=int(self.cfg["conveyor_y"]),
            radius_x=int(self.cfg.get("pizza_radius_x", round(game_w * 0.165))),
            radius_y=int(self.cfg.get("pizza_radius_y", round(game_h * 0.173))),
            bbox=(0, 0, 0, 0),
            fully_visible=True,
        )

    def _tracked_drop_geometry(self, fallback_x: int) -> PizzaGeometry:
        """Predict the active pizza at release time or abort before a wrong drop."""
        previous_at = getattr(self, "_last_pizza_geometry_at", None)
        if previous_at is not None:
            age = max(0.0, time.perf_counter() - previous_at)
            max_age = float(self.cfg.get("pizza_tracking_cache_max_age", 6.0))
            if age > max_age:
                raise PizzaTrackingError("active pizza tracking expired")

        geometry = self._fresh_pizza_geometry(fallback_x)
        speed = float(getattr(
            self,
            "_last_conveyor_speed",
            self.cfg.get("conveyor_speed_default", 90.0),
        ))
        lead_seconds = float(self.cfg.get("topping_drop_lead_seconds", 0.035))
        predicted_x = round(geometry.center_x + max(0.0, speed) * lead_seconds)
        game_left = int(self.cfg["game_top_left"][0])
        if predicted_x < game_left + 20:
            raise PizzaTrackingError("tracked pizza is left of the safe work area")
        if predicted_x > self._max_safe_drop_x(geometry.radius_x):
            raise PizzaTrackingError("tracked pizza reached the right edge")

        return PizzaGeometry(
            center_x=predicted_x,
            center_y=geometry.center_y,
            radius_x=geometry.radius_x,
            radius_y=geometry.radius_y,
            bbox=geometry.bbox,
            fully_visible=geometry.fully_visible,
        )

    def _measure_pizza_motion(self, fallback_x: int) -> tuple[PizzaGeometry, float, float]:
        """Measure velocity from several frames using a robust median slope."""
        sample_count = max(3, min(7, int(self.cfg.get("motion_sample_count", 5))))
        sample_interval = float(self.cfg.get("motion_sample_interval", 0.04))
        samples: list[tuple[float, PizzaGeometry]] = []
        geometry = self._fresh_pizza_geometry(fallback_x)
        samples.append((time.perf_counter(), geometry))
        for _index in range(sample_count - 1):
            time.sleep(sample_interval)
            geometry = self._fresh_pizza_geometry(geometry.center_x)
            samples.append((time.perf_counter(), geometry))

        slopes: list[float] = []
        for left_index in range(len(samples)):
            left_time, left_geometry = samples[left_index]
            for right_time, right_geometry in samples[left_index + 1:]:
                dt = right_time - left_time
                if dt <= 0.001:
                    continue
                slope = (right_geometry.center_x - left_geometry.center_x) / dt
                if 15.0 <= slope <= 650.0:
                    slopes.append(slope)
        previous_speed = float(getattr(
            self,
            "_last_conveyor_speed",
            self.cfg.get("conveyor_speed_default", 90.0),
        ))
        if slopes:
            speed = max(float(median(slopes)), previous_speed * 0.70)
        else:
            speed = previous_speed
        last_time, last_geometry = samples[-1]
        self._last_pizza_geometry = last_geometry
        self._last_pizza_geometry_at = last_time
        self._last_conveyor_speed = speed
        return last_geometry, speed, last_time

    def _sauce_nozzle_offset(self, is_hot: bool) -> tuple[int, int]:
        """Offset from the calibrated bottle click point to its dispensing tip."""
        key = "sauce_hot_nozzle_offset" if is_hot else "sauce_regular_nozzle_offset"
        configured = self.cfg.get(key)
        if configured and len(configured) == 2:
            return int(configured[0]), int(configured[1])

        game_w = self.cfg["game_bottom_right"][0] - self.cfg["game_top_left"][0]
        game_h = self.cfg["game_bottom_right"][1] - self.cfg["game_top_left"][1]
        if is_hot:
            return round(game_w * 0.012), round(game_h * 0.182)
        return round(game_w * 0.010), round(game_h * 0.158)

    @staticmethod
    def _build_sauce_path(
        radius_x: int,
        radius_y: int,
        fill_x: float = 0.84,
        fill_y: float = 0.73,
        edge_laps: int = 0,
        fill_points: int = 64,
        edge_points: int = 20,
    ) -> list[tuple[int, int]]:
        """Build a frame-sampling-resistant fill at the nozzle tip.

        A sunflower distribution keeps every time-decimated subset spread
        across the pizza.  This is more reliable than sending thousands of
        adjacent raster points that a browser can coalesce between frames.
        A uniformly sampled edge pass then closes the rim.
        """
        path: list[tuple[int, int]] = [(0, 0)]
        work_rx = max(24, round(radius_x * fill_x))
        work_ry = max(18, round(radius_y * fill_y))
        fill_points = max(24, int(fill_points))
        golden_angle = math.pi * (3.0 - math.sqrt(5.0))
        for index in range(fill_points):
            radius = math.sqrt((index + 0.5) / fill_points)
            angle = index * golden_angle
            path.append((
                round(work_rx * radius * math.cos(angle)),
                round(work_ry * radius * math.sin(angle)),
            ))

        total_edge_points = max(0, int(edge_laps)) * max(12, int(edge_points))
        for index in range(total_edge_points):
            progress = index / max(1, total_edge_points)
            angle = math.pi / 2.0 + progress * 2.0 * math.pi
            path.append((
                round(work_rx * math.cos(angle)),
                round(work_ry * math.sin(angle)),
            ))
        return path

    @staticmethod
    def _set_cursor(x: int, y: int) -> None:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        # MOUSEEVENTF_MOVE | MOUSEEVENTF_MOVE_NOCOALESCE.  Explicitly request
        # delivery of each planned move instead of allowing Windows to merge
        # a burst into one late cursor position.
        ctypes.windll.user32.mouse_event(0x2001, 0, 0, 0, 0)

    def _sauce_coverage_ratio(self, game_img: np.ndarray, geometry: PizzaGeometry) -> float:
        """Estimate red/orange coverage inside the crust after the bottle is released."""
        tl = self.cfg["game_top_left"]
        cx = int(
            geometry.center_x
            + float(self.cfg.get("sauce_center_offset_x", 3))
            - tl[0]
        )
        cy = int(
            geometry.center_y
            + float(self.cfg.get("sauce_center_offset_y", -6))
            - tl[1]
        )
        rx = max(10, int(geometry.radius_x * 0.88))
        ry = max(10, int(geometry.radius_y * 0.80))

        ellipse = np.zeros(game_img.shape[:2], dtype=np.uint8)
        if not (0 <= cx < game_img.shape[1] and 0 <= cy < game_img.shape[0]):
            return 0.0
        cv2.ellipse(ellipse, (cx, cy), (rx, ry), 0, 0, 360, 255, -1)
        hsv = cv2.cvtColor(game_img, cv2.COLOR_BGR2HSV)
        warm = cv2.inRange(hsv, np.array([0, 105, 95]), np.array([24, 255, 255]))
        pixels = cv2.countNonZero(ellipse)
        if pixels == 0:
            return 0.0
        return cv2.countNonZero(cv2.bitwise_and(warm, ellipse)) / pixels

    def apply_sauce(self, is_hot: bool, start_pizza_x: int) -> bool:
        """Coat the fully visible moving dough using the bottle's actual nozzle."""
        bottle_key = "sauce_hot" if is_hot else "sauce_regular"
        src = self.cfg[bottle_key]
        geometry, conveyor_speed, tracked_at = self._measure_pizza_motion(start_pizza_x)
        nozzle_dx, nozzle_dy = self._sauce_nozzle_offset(is_hot)
        normal_points = int(self.cfg.get("sauce_fill_points", 64))
        fast_points = int(self.cfg.get("fast_sauce_fill_points", 40))
        fill_points = round(self._speed_scaled(normal_points, fast_points, conveyor_speed))
        normal_step_delay = float(self.cfg.get("sauce_motion_step_delay", 0.029))
        fast_step_delay = float(self.cfg.get("fast_sauce_motion_step_delay", 0.017))
        step_delay = self._speed_scaled(normal_step_delay, fast_step_delay, conveyor_speed)
        path = self._build_sauce_path(
            geometry.radius_x,
            geometry.radius_y,
            fill_x=float(self.cfg.get("sauce_fill_x_factor", 0.84)),
            fill_y=float(self.cfg.get("sauce_fill_y_factor", 0.73)),
            edge_laps=int(self.cfg.get("sauce_edge_laps", 0)),
            fill_points=fill_points,
            edge_points=int(self.cfg.get("sauce_edge_points", 20)),
        )
        sauce_center_dx = float(self.cfg.get("sauce_center_offset_x", 3))
        sauce_center_dy = float(self.cfg.get("sauce_center_offset_y", -6))
        normal_reanchor = float(self.cfg.get("sauce_reanchor_interval", 0.40))
        fast_reanchor = float(self.cfg.get("fast_sauce_reanchor_interval", 0.22))
        reanchor_interval = self._speed_scaled(normal_reanchor, fast_reanchor, conveyor_speed)

        # Reserve enough of the remaining conveyor time for cheese and every
        # requested extra.  If necessary, trim the distributed sauce pass, but
        # never go below the minimum that reliably covers the dough.
        sauce_right_limit = int(self.cfg["game_bottom_right"][0]) - max(
            40,
            round(geometry.radius_x * float(self.cfg.get("sauce_right_margin_factor", 0.84))),
        )
        sauce_runway_seconds = max(
            0.0,
            (sauce_right_limit - geometry.center_x) / max(1.0, conveyor_speed),
        )
        remaining_drops = max(1, int(getattr(self, "_active_remaining_drops", 1)))
        normal_drop_seconds = float(self.cfg.get("estimated_drop_seconds", 0.30))
        fast_drop_seconds = float(self.cfg.get("fast_estimated_drop_seconds", 0.24))
        drop_seconds = self._speed_scaled(
            normal_drop_seconds, fast_drop_seconds, conveyor_speed
        )
        pickup_delay = self._speed_scaled(
            float(self.cfg.get("sauce_pickup_delay", 0.075)),
            float(self.cfg.get("fast_sauce_pickup_delay", 0.05)),
            conveyor_speed,
        )
        fixed_sauce_seconds = pickup_delay + 0.055 + 0.10 + 0.06
        reserve_seconds = remaining_drops * drop_seconds + float(
            self.cfg.get("fast_action_reserve", 0.18)
        )
        drop_runway_seconds = max(
            0.0,
            (self._max_safe_drop_x(geometry.radius_x) - geometry.center_x)
            / max(1.0, conveyor_speed),
        )
        available_sweep = min(
            sauce_runway_seconds - fixed_sauce_seconds,
            drop_runway_seconds - fixed_sauce_seconds - reserve_seconds,
        )
        minimum_points = max(24, int(self.cfg.get("minimum_sauce_fill_points", 24)))
        maximum_path_points = math.floor(available_sweep / max(0.001, step_delay))
        if maximum_path_points < minimum_points + 1:
            self.log("  ⚠  Pizza moved too far for a safe complete recipe — skipping it")
            return False
        if maximum_path_points < len(path):
            fill_points = max(minimum_points, maximum_path_points - 1)
            path = self._build_sauce_path(
                geometry.radius_x,
                geometry.radius_y,
                fill_x=float(self.cfg.get("sauce_fill_x_factor", 0.84)),
                fill_y=float(self.cfg.get("sauce_fill_y_factor", 0.73)),
                edge_laps=0,
                fill_points=fill_points,
                edge_points=int(self.cfg.get("sauce_edge_points", 20)),
            )

        if self.debug:
            self.log(
                f"  Belt {conveyor_speed:.0f}px/s → {len(path)} sauce points "
                f"at {step_delay * 1000:.0f}ms"
            )

        pyautogui.moveTo(src[0], src[1])
        time.sleep(pickup_delay)
        pyautogui.mouseDown()
        completed_path = True
        try:
            time.sleep(0.055)
            first_x, first_y = path[0]
            predicted_x = geometry.center_x + conveyor_speed * (time.perf_counter() - tracked_at)
            pyautogui.moveTo(
                predicted_x + sauce_center_dx + first_x - nozzle_dx,
                geometry.center_y + sauce_center_dy + first_y - nozzle_dy,
                duration=0.10,
            )

            next_reanchor = time.perf_counter() + reanchor_interval
            for offset_x, offset_y in path:
                if not self.running or self.paused:
                    break
                now = time.perf_counter()
                if reanchor_interval > 0 and now >= next_reanchor:
                    predicted_for_match = (
                        geometry.center_x
                        + conveyor_speed * (time.perf_counter() - tracked_at)
                    )
                    observed = self._detect_pizza_near(
                        self.grab_game(), predicted_for_match
                    )
                    observed_at = time.perf_counter()
                    predicted_before_anchor = (
                        geometry.center_x
                        + conveyor_speed * (observed_at - tracked_at)
                    )
                    max_anchor_error = max(30.0, geometry.radius_x * 0.35)
                    if (
                        observed is not None
                        and abs(observed.center_x - predicted_before_anchor) <= max_anchor_error
                    ):
                        anchor_dt = observed_at - tracked_at
                        if anchor_dt > 0.05:
                            observed_speed = (observed.center_x - geometry.center_x) / anchor_dt
                            if 15.0 <= observed_speed <= 650.0:
                                conveyor_speed = 0.35 * conveyor_speed + 0.65 * observed_speed
                        geometry = observed
                        tracked_at = observed_at
                    next_reanchor = observed_at + reanchor_interval
                    now = observed_at
                predicted_x = geometry.center_x + conveyor_speed * (now - tracked_at)
                if predicted_x > sauce_right_limit:
                    completed_path = False
                    break
                self._set_cursor(
                    round(predicted_x + sauce_center_dx + offset_x - nozzle_dx),
                    round(geometry.center_y + sauce_center_dy + offset_y - nozzle_dy),
                )
                time.sleep(step_delay)
        finally:
            pyautogui.mouseUp()
        time.sleep(0.06)

        finished_at = time.perf_counter()
        finished_x = round(geometry.center_x + conveyor_speed * (finished_at - tracked_at))
        self._last_pizza_geometry = PizzaGeometry(
            center_x=finished_x,
            center_y=geometry.center_y,
            radius_x=geometry.radius_x,
            radius_y=geometry.radius_y,
            bbox=geometry.bbox,
            fully_visible=geometry.fully_visible,
        )
        self._last_pizza_geometry_at = finished_at
        self._last_conveyor_speed = conveyor_speed

        if not completed_path:
            self.log("  ⚠  Tracked pizza reached the sauce boundary — stopping this recipe")
            return False

        # Diagnostic feedback catches calibration/server differences without
        # risking an over-sauce splat by blindly repeating a completed pass.
        after = self.grab_game()
        after_geometry = self.detect_pizza_geometry(after)
        if after_geometry is not None:
            coverage = self._sauce_coverage_ratio(after, after_geometry)
            if self.debug:
                self.log(f"  Sauce coverage estimate: {coverage:.1%}")
            if coverage < float(self.cfg.get("sauce_coverage_warn_threshold", 0.55)):
                self.log("  Warning: sauce coverage looks low; check nozzle calibration.")
        return True

    def apply_cheese(self, start_pizza_x: int) -> bool:
        """Drag shredded cheese from bin onto the pizza."""
        src = self.cfg["topping_cheese"]
        pickup_delay = self._speed_scaled(
            float(self.cfg.get("click_delay", 0.06)),
            float(self.cfg.get("fast_click_delay", 0.04)),
        )
        settle_delay = self._speed_scaled(
            float(self.cfg.get("post_topping_delay", 0.06)),
            float(self.cfg.get("fast_post_topping_delay", 0.03)),
        )

        pyautogui.moveTo(src[0], src[1])
        time.sleep(0.02)
        pyautogui.mouseDown()
        try:
            time.sleep(pickup_delay)
            geometry = self._tracked_drop_geometry(start_pizza_x)
            pyautogui.moveTo(geometry.center_x, geometry.center_y, duration=0.08)
            time.sleep(0.02)
        except PizzaTrackingError as exc:
            self.log(f"  ⚠  Cheese cancelled: {exc}")
            return False
        finally:
            pyautogui.mouseUp()
        time.sleep(settle_delay)
        return True

    def apply_topping_pieces(
        self, topping: Topping, start_pizza_x: int, count: int = 5
    ) -> bool:
        """Drag multiple pieces (default 5) of the topping onto the pizza."""
        src = self.cfg[topping.value]
        pickup_delay = self._speed_scaled(
            float(self.cfg.get("click_delay", 0.06)),
            float(self.cfg.get("fast_click_delay", 0.04)),
        )
        settle_delay = self._speed_scaled(
            float(self.cfg.get("post_topping_delay", 0.06)),
            float(self.cfg.get("fast_post_topping_delay", 0.03)),
        )
        if count <= 1:
            relative_offsets = [(0.0, 0.0)]
        elif count == 2:
            relative_offsets = [(-0.24, 0.0), (0.24, 0.0)]
        else:
            relative_offsets = [
                (-0.28, -0.18), (0.28, -0.18),
                (-0.25, 0.18), (0.25, 0.18), (0.0, 0.0),
            ]

        for i in range(min(count, len(relative_offsets))):
            if not self.running or self.paused:
                return False

            pyautogui.moveTo(src[0], src[1])
            time.sleep(0.02)
            pyautogui.mouseDown()
            try:
                time.sleep(pickup_delay)
                geometry = self._tracked_drop_geometry(start_pizza_x)
                rel_x, rel_y = relative_offsets[i]
                off_x = round(geometry.radius_x * rel_x)
                off_y = round(geometry.radius_y * rel_y)
                pyautogui.moveTo(
                    geometry.center_x + off_x,
                    geometry.center_y + off_y,
                    duration=0.07,
                )
                time.sleep(0.02)
            except PizzaTrackingError as exc:
                self.log(f"  ⚠  {topping.name} cancelled: {exc}")
                return False
            finally:
                pyautogui.mouseUp()
            time.sleep(settle_delay)
        return True

    def make_pizza(self, pizza_x: int) -> bool:
        """Read the current order and apply sauce, cheese, and all toppings via drag-and-drop."""
        self._last_make_failure_terminal = False
        try:
            order = self.detect_order()
        except OrderDetectionError as exc:
            self.log(f"  Order unreadable ({exc}); skipping this pizza safely.")
            return False

        self.current_order_str = order.description
        self.log(f"  📋  Order  →  {self.current_order_str}")
        self._active_remaining_drops = 1 + sum(order.extras.values())

        # 1. Apply Sauce
        is_hot = order.sauce is Topping.HOT_SAUCE
        if self.apply_sauce(is_hot, pizza_x) is False:
            self._last_make_failure_terminal = True
            return False

        # 2. Apply Cheese
        if not self.running or self.paused:
            return False
        if self.apply_cheese(pizza_x) is False:
            self._last_make_failure_terminal = True
            return False

        if not self.running or self.paused:
            return False

        # 3. Apply the exact requested piece count.  Single-topping orders
        # need 5, dual orders need 2 each, and supreme orders need 1 each.
        for extra in EXTRA_TOPPINGS:
            if extra not in order.extras:
                continue
            if not self.running or self.paused:
                return False
            if self.apply_topping_pieces(
                extra, pizza_x, count=order.extras[extra]
            ) is False:
                self._last_make_failure_terminal = True
                return False
            if not self.running or self.paused:
                return False

        self.pizzas_made += 1
        self.log(f"  ✅  Pizza #{self.pizzas_made} complete!")
        return True

    # ── hotkey callbacks ─────────────────────────────────────────

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self.status = "Paused"
            self.log("\n  [⏸  PAUSED]\n")
        else:
            self.status = "Running"
            self.log("\n  [▶  RUNNING]\n")

    def _stop(self) -> None:
        self.running = False
        self.status = "Stopped"
        self.log("\n  [⏹  STOPPING…]\n")

    # ── main loop ────────────────────────────────────────────────

    def run(self, setup_keyboard: bool = True) -> None:
        mode_label = "TEST — no clicks" if self.test_mode else "ACTIVE"
        self.log("")
        self.log("=" * 52)
        self.log(f"  🍕  PIZZATRON 3000 BOT  [{mode_label}]")
        self.log("=" * 52)

        if setup_keyboard:
            self.log("")
            self.log("  F2   →  Start / Pause")
            self.log("  ESC  →  Stop & quit")
            if self.debug:
                self.log(f"  📁   →  Debug frames in  {DEBUG_DIR}/")
            self.log("")
            self.log("  Press F2 when the game is ready…")
            self.log("")

        self.running = True
        if setup_keyboard:
            self.paused = True
            self.status = "Idle"
        else:
            # GUI controls paused/running state directly
            pass

        if setup_keyboard:
            keyboard.on_press_key("F2", lambda _: self._toggle_pause())
            keyboard.on_press_key("escape", lambda _: self._stop())

        cooldown_end: float = 0
        idle_since: float = time.time()

        try:
            while self.running:
                # ---- paused ----
                if self.paused:
                    time.sleep(0.1)
                    idle_since = time.time()
                    continue

                self.frame_idx += 1
                now = time.time()

                # ---- cooldown after completing a pizza ----
                if now < cooldown_end:
                    time.sleep(0.05)
                    continue

                # ---- end screen / reward / room navigation ----
                game_img = self.grab_game()
                if (
                    self.cfg.get("auto_restart", True)
                    and self._handle_restart_flow(game_img, now)
                ):
                    idle_since = now
                    time.sleep(self.cfg.get("scan_interval", 0.05))
                    continue

                # ---- detect pizza on conveyor ----
                geometry = self.detect_pizza_geometry(game_img)

                if geometry is not None:
                    idle_since = now
                    # The original target was only 110 px inside the game,
                    # while the measured pizza radius is ~212 px.  Never act
                    # until the entire crust is reachable on screen.
                    game_left = int(self.cfg["game_top_left"][0])
                    configured_target = int(self.cfg["target_x"])
                    safe_target = max(
                        configured_target,
                        game_left + geometry.radius_x + int(self.cfg.get("pizza_edge_margin", 12)),
                    )
                    zone = self.cfg.get("target_zone_radius", 130)

                    if (
                        geometry.fully_visible
                        and safe_target <= geometry.center_x <= safe_target + zone
                    ):
                        pizza_x = geometry.center_x
                        self._last_pizza_geometry = geometry
                        self._last_pizza_geometry_at = time.perf_counter()
                        self.log(f"\n  🍕  Pizza detected at x={pizza_x}")

                        if self.test_mode:
                            try:
                                recipe = self.detect_order()
                                self.current_order_str = recipe.description
                                self.log(f"  📋  Would make: {recipe.description}")
                            except OrderDetectionError as exc:
                                self.log(f"  Order unreadable ({exc}); no action taken.")
                            cooldown_end = now + 1.5
                        else:
                            # A single capture can coincide with the order-card
                            # transition.  Retry only while this same pizza is
                            # still in the safe action window.
                            retry_deadline = time.time() + float(
                                self.cfg.get("order_retry_window", 0.45)
                            )
                            made = False
                            while self.running and not self.paused and time.time() < retry_deadline:
                                made = self.make_pizza(pizza_x)
                                if made or getattr(self, "_last_make_failure_terminal", False):
                                    break
                                time.sleep(0.04)
                                retry_geometry = self.detect_pizza_geometry(self.grab_game())
                                if (
                                    retry_geometry is None
                                    or not retry_geometry.fully_visible
                                    or retry_geometry.center_x > safe_target + zone
                                ):
                                    break
                                pizza_x = retry_geometry.center_x
                            terminal_failure = bool(getattr(
                                self, "_last_make_failure_terminal", False
                            ))
                            if not made and self.running and not self.paused:
                                if terminal_failure:
                                    self.log(
                                        "  Current pizza was abandoned safely; "
                                        "no ingredients will be placed on the next one."
                                    )
                                else:
                                    self.log(
                                        "  Could not read a stable order before the pizza moved on."
                                    )
                            # Ingredient application already takes long enough
                            # for the handled pizza to leave the action zone.
                            # A blind clear-wait can see the *next* pizza and
                            # suppress it for another 1.2 seconds, so return to
                            # scanning immediately after the short cooldown.
                            cooldown_key = (
                                "aborted_pizza_cooldown"
                                if terminal_failure
                                else "post_pizza_cooldown"
                            )
                            cooldown_default = 0.45 if terminal_failure else 0.2
                            cooldown_end = time.time() + float(
                                self.cfg.get(cooldown_key, cooldown_default)
                            )
                else:
                    # No pizza for 20s → probably game over
                    if now - idle_since > 20:
                        self.log("\n  ⏰  No pizza detected for 20 s — game over?")
                        idle_since = now  # reset so msg doesn't spam

                time.sleep(self.cfg.get("scan_interval", 0.05))

        except KeyboardInterrupt:
            pass
        finally:
            if setup_keyboard:
                keyboard.unhook_all()
            self.status = "Stopped"
            self.log("")
            self.log("=" * 52)
            self.log(f"  🍕  Total pizzas made: {self.pizzas_made}")
            self.log("=" * 52)
            self.log("")


# ═══════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser(description="Pizzatron 3000 Bot")
    ap.add_argument("--test", action="store_true",
                    help="Test detection without clicking")
    ap.add_argument("--debug", action="store_true",
                    help="Save annotated debug frames")
    ap.add_argument("--config", default=CONFIG_FILE,
                    help="Path to config JSON (default: config.json)")
    args = ap.parse_args()

    bot = PizzaBot(
        config_path=args.config,
        debug=args.debug,
        test_mode=args.test,
    )
    bot.run()


if __name__ == "__main__":
    main()
