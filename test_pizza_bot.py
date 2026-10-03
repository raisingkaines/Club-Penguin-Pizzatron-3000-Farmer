"""Regression tests for the production Pizzatron detector and action planner."""

from __future__ import annotations

import json
import math
import os
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from pizza_bot import (
    DetectedOrder,
    OrderDetectionError,
    PizzaBot,
    PizzaGeometry,
    PizzaTrackingError,
    Topping,
)


HERE = os.path.dirname(os.path.abspath(__file__))


def bare_bot() -> PizzaBot:
    """Create a bot without opening an MSS session or installing hotkeys."""
    bot = object.__new__(PizzaBot)
    with open(
        os.path.join(HERE, "config.example.json"), encoding="utf-8"
    ) as config_file:
        bot.cfg = json.load(config_file)
    bot.debug = False
    bot.frame_idx = 1
    bot.running = True
    bot.paused = False
    bot.pizzas_made = 0
    bot.current_order_str = ""
    bot._log_fn = lambda _message: None
    return bot


class OrderRecognitionTests(unittest.TestCase):
    def test_saved_real_plain_order_fixture(self) -> None:
        board = cv2.imread(
            os.path.join(HERE, "test_fixtures", "plain_order_board.png")
        )
        self.assertIsNotNone(board)
        order = bare_bot()._parse_order_board(board)
        self.assertIs(order.sauce, Topping.SAUCE)
        self.assertEqual(order.extras, {})

    def test_regular_and_hot_sauce_are_separated(self) -> None:
        regular = PizzaBot._classify_order_lines([47, 79])
        hot = PizzaBot._classify_order_lines([47, 68])
        compact_hot = PizzaBot._classify_order_lines([47, 58])
        self.assertIs(regular.sauce, Topping.SAUCE)
        self.assertIs(hot.sauce, Topping.HOT_SAUCE)
        self.assertIs(compact_hot.sauce, Topping.HOT_SAUCE)

    def test_ambiguous_sauce_is_not_silently_defaulted(self) -> None:
        with self.assertRaises(OrderDetectionError):
            PizzaBot._classify_order_lines([47, 73])

    def test_plain_and_quantity_prefixed_shrimp(self) -> None:
        plain = PizzaBot._classify_order_lines([47, 68, 45])
        prefixed = PizzaBot._classify_order_lines([47, 79, 59])
        self.assertEqual(plain.extras, {Topping.SHRIMP: 5})
        self.assertEqual(prefixed.extras, {Topping.SHRIMP: 5})

    def test_exact_counts_for_dual_and_supreme_orders(self) -> None:
        dual = PizzaBot._classify_order_lines([47, 79, 52, 45])
        supreme = PizzaBot._classify_order_lines([47, 68, 52, 45, 38, 30])
        self.assertEqual(
            dual.extras,
            {Topping.SEAWEED: 2, Topping.SHRIMP: 2},
        )
        self.assertEqual(
            supreme.extras,
            {
                Topping.SEAWEED: 1,
                Topping.SHRIMP: 1,
                Topping.SQUID: 1,
                Topping.FISH: 1,
            },
        )

    def test_three_extra_rows_are_rejected_as_an_invalid_recipe(self) -> None:
        with self.assertRaises(OrderDetectionError):
            PizzaBot._classify_order_lines([47, 79, 52, 45, 38])

    def test_same_width_squid_and_prefixed_fish_need_visual_evidence(self) -> None:
        with self.assertRaises(OrderDetectionError):
            PizzaBot._classify_order_lines([47, 79, 38])

        squid = PizzaBot._classify_order_lines(
            [47, 79, 38],
            {Topping.SQUID: 0.02},
        )
        fish = PizzaBot._classify_order_lines(
            [47, 79, 38],
            {Topping.FISH: 0.02},
        )
        self.assertEqual(squid.extras, {Topping.SQUID: 5})
        self.assertEqual(fish.extras, {Topping.FISH: 5})

    def test_overlapping_width_with_weak_squid_preview_chooses_squid(self) -> None:
        order = PizzaBot._classify_order_lines(
            [47, 79, 52],
            {Topping.SQUID: 0.0035},
        )

        self.assertEqual(order.extras, {Topping.SQUID: 5})

    def test_overlapping_seaweed_squid_width_without_colour_is_rejected(self) -> None:
        with self.assertRaises(OrderDetectionError):
            PizzaBot._classify_order_lines([47, 79, 52])

    def test_overlapping_width_with_weak_seaweed_preview_chooses_seaweed(self) -> None:
        order = PizzaBot._classify_order_lines(
            [47, 79, 52],
            {Topping.SEAWEED: 0.0035},
        )

        self.assertEqual(order.extras, {Topping.SEAWEED: 5})

    def test_live_squid_preview_overrides_misleading_seaweed_width(self) -> None:
        # Captured from the running client when a 5-SQUID order was wrongly
        # classified as SEAWEED.  The rendered text width is misleading, but
        # the blue preview evidence is unambiguous.
        order = PizzaBot._classify_order_lines(
            [46.9, 69.2, 77.9],
            {
                Topping.SEAWEED: 0.00000,
                Topping.SQUID: 0.05133,
                Topping.FISH: 0.00597,
                Topping.SHRIMP: 0.00055,
            },
        )

        self.assertIs(order.sauce, Topping.HOT_SAUCE)
        self.assertEqual(order.extras, {Topping.SQUID: 5})

    def test_multiframe_vote_does_not_accept_one_lucky_parse(self) -> None:
        bot = bare_bot()
        bot.cfg["order_sample_interval"] = 0
        bot.grab_order_board = lambda: np.zeros((10, 10, 3), dtype=np.uint8)
        results = iter(
            [
                DetectedOrder(Topping.SAUCE),
                OrderDetectionError("blurred"),
                OrderDetectionError("occluded"),
            ]
        )

        def parse(_board):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        bot._parse_order_board = parse
        with self.assertRaises(OrderDetectionError):
            bot.detect_order()

    def test_low_confidence_order_is_rejected(self) -> None:
        bot = bare_bot()
        bot.cfg["order_sample_interval"] = 0
        bot.grab_order_board = lambda: np.zeros((10, 10, 3), dtype=np.uint8)
        bot._parse_order_board = lambda _board: DetectedOrder(
            Topping.HOT_SAUCE,
            confidence=0.40,
        )
        with self.assertRaises(OrderDetectionError):
            bot.detect_order()


class PizzaGeometryTests(unittest.TestCase):
    def test_long_thin_conveyor_colour_is_not_a_pizza(self) -> None:
        bot = bare_bot()
        image = np.zeros((798, 1283, 3), dtype=np.uint8)
        cv2.rectangle(image, (0, 545), (740, 570), (0, 140, 255), -1)
        self.assertIsNone(bot.detect_pizza_geometry(image))

    def test_full_dough_ellipse_is_detected(self) -> None:
        bot = bare_bot()
        image = np.zeros((798, 1283, 3), dtype=np.uint8)
        cv2.ellipse(image, (310, 606), (212, 138), 0, 0, 360, (145, 190, 230), -1)
        geometry = bot.detect_pizza_geometry(image)
        self.assertIsNotNone(geometry)
        assert geometry is not None
        self.assertTrue(geometry.fully_visible)
        self.assertAlmostEqual(geometry.center_x, 627, delta=8)

    def test_stale_tracking_cache_is_not_reused_for_a_new_pizza(self) -> None:
        bot = bare_bot()
        bot.grab_game = lambda: np.zeros((798, 1283, 3), dtype=np.uint8)
        bot.detect_pizza_geometry = lambda _image: None
        bot._last_pizza_geometry = PizzaGeometry(
            1000, 780, 212, 138, (0, 0, 0, 0), True
        )
        bot._last_pizza_geometry_at = 0.0
        bot._last_conveyor_speed = 90.0
        with patch("pizza_bot.time.perf_counter", return_value=10.0):
            geometry = bot._fresh_pizza_geometry(620)
        self.assertEqual(geometry.center_x, 620)

    def test_fresh_tracking_accepts_original_pizza_near_prediction(self) -> None:
        bot = bare_bot()
        bot.grab_game = lambda: np.zeros((798, 1283, 3), dtype=np.uint8)
        observed = PizzaGeometry(948, 780, 212, 138, (0, 0, 0, 0), True)
        bot.detect_pizza_geometry = lambda _image: observed
        bot._last_pizza_geometry = PizzaGeometry(
            900, 780, 212, 138, (0, 0, 0, 0), True
        )
        bot._last_pizza_geometry_at = 10.0
        bot._last_conveyor_speed = 100.0

        with patch("pizza_bot.time.perf_counter", return_value=10.5):
            geometry = bot._fresh_pizza_geometry(620)

        self.assertIs(geometry, observed)
        self.assertIs(bot._last_pizza_geometry, observed)
        self.assertEqual(bot._last_pizza_geometry_at, 10.5)

    def test_fresh_tracking_rejects_far_left_new_pizza(self) -> None:
        bot = bare_bot()
        bot.grab_game = lambda: np.zeros((798, 1283, 3), dtype=np.uint8)
        newcomer = PizzaGeometry(560, 780, 212, 138, (0, 0, 0, 0), True)
        bot.detect_pizza_geometry = lambda _image: newcomer
        original = PizzaGeometry(900, 780, 212, 138, (0, 0, 0, 0), True)
        bot._last_pizza_geometry = original
        bot._last_pizza_geometry_at = 10.0
        bot._last_conveyor_speed = 100.0

        with patch("pizza_bot.time.perf_counter", return_value=10.5):
            geometry = bot._fresh_pizza_geometry(620)

        self.assertEqual(geometry.center_x, 950)
        self.assertIs(bot._last_pizza_geometry, original)
        self.assertEqual(bot._last_pizza_geometry_at, 10.0)

    def test_far_newcomer_keeps_prediction_past_old_fallback_limit(self) -> None:
        """Never snap an old recipe back onto the next pizza at the entry."""
        bot = bare_bot()
        bot.grab_game = lambda: np.zeros((798, 1283, 3), dtype=np.uint8)
        newcomer = PizzaGeometry(560, 780, 212, 138, (0, 0, 0, 0), True)
        bot.detect_pizza_geometry = lambda _image: newcomer
        original = PizzaGeometry(1000, 780, 212, 138, (0, 0, 0, 0), True)
        bot._last_pizza_geometry = original
        bot._last_pizza_geometry_at = 10.0
        bot._last_conveyor_speed = 200.0

        # Prediction is 500 px ahead of the prior observation and 880 px
        # away from fallback_x.  The former implementation discarded that
        # prediction once it exceeded a radius-based fallback limit and
        # returned x=620, which is exactly where a newcomer can be waiting.
        with patch("pizza_bot.time.perf_counter", return_value=12.5):
            geometry = bot._fresh_pizza_geometry(620)

        self.assertEqual(geometry.center_x, 1500)
        self.assertIs(bot._last_pizza_geometry, original)
        self.assertEqual(bot._last_pizza_geometry_at, 10.0)

    def test_tracked_drop_aborts_at_right_edge(self) -> None:
        bot = bare_bot()
        bot._last_pizza_geometry_at = None
        bot._last_conveyor_speed = 0.0
        right_limit = bot._max_safe_drop_x(212)
        bot._fresh_pizza_geometry = lambda _fallback: PizzaGeometry(
            right_limit + 1, 780, 212, 138, (0, 0, 0, 0), False
        )

        with self.assertRaisesRegex(PizzaTrackingError, "right edge"):
            bot._tracked_drop_geometry(620)

    def test_release_lead_aborts_before_crossing_right_edge(self) -> None:
        bot = bare_bot()
        bot._last_pizza_geometry_at = None
        bot._last_conveyor_speed = 320.0
        right_limit = bot._max_safe_drop_x(212)
        # The detected center is still safe, but the configured 100 ms
        # pickup-to-release projection moves it beyond the drop boundary.
        bot._fresh_pizza_geometry = lambda _fallback: PizzaGeometry(
            right_limit - 20, 780, 212, 138, (0, 0, 0, 0), True
        )

        with self.assertRaisesRegex(PizzaTrackingError, "right edge"):
            bot._tracked_drop_geometry(620)

    def test_active_track_prefers_nearest_of_two_pizza_contours(self) -> None:
        bot = bare_bot()
        frame = np.zeros((798, 1283, 3), dtype=np.uint8)
        dough = (145, 190, 230)
        cv2.ellipse(frame, (300, 606), (180, 65), 0, 0, 360, dough, -1)
        cv2.ellipse(frame, (900, 606), (180, 65), 0, 0, 360, dough, -1)
        preferred_x = bot.cfg["game_top_left"][0] + 900

        geometry = bot._detect_pizza_near(frame, preferred_x)

        self.assertIsNotNone(geometry)
        assert geometry is not None
        self.assertAlmostEqual(geometry.center_x, preferred_x, delta=8)


class RestartScreenDetectionTests(unittest.TestCase):
    @staticmethod
    def _load_fixture(name: str) -> np.ndarray:
        image = cv2.imread(os.path.join(HERE, "test_fixtures", name))
        if image is None:
            raise AssertionError(f"Could not load restart fixture: {name}")
        return image

    def test_detects_end_score_screen(self) -> None:
        frame = self._load_fixture("end_score.png")

        self.assertEqual(bare_bot()._detect_restart_screen(frame), "score")

    def test_reward_popup_takes_precedence_over_score_behind_it(self) -> None:
        # The captured reward popup still exposes the orange Done button and
        # most of the score screen, so classification order is significant.
        frame = self._load_fixture("reward_popup.png")

        self.assertEqual(bare_bot()._detect_restart_screen(frame), "reward")

    def test_detects_pizza_parlor_after_leaving_minigame(self) -> None:
        frame = self._load_fixture("pizza_parlor.png")

        self.assertEqual(bare_bot()._detect_restart_screen(frame), "parlor")

    def test_play_confirmation_takes_precedence_over_parlor_behind_it(self) -> None:
        frame = self._load_fixture("play_confirmation.png")

        self.assertEqual(bare_bot()._detect_restart_screen(frame), "confirm")

    def test_detects_pizzatron_start_screen(self) -> None:
        frame = self._load_fixture("pizzatron_start.png")

        self.assertEqual(bare_bot()._detect_restart_screen(frame), "start")

    def test_restart_detection_is_resolution_independent(self) -> None:
        cases = {
            "end_score.png": "score",
            "reward_popup.png": "reward",
            "pizza_parlor.png": "parlor",
            "play_confirmation.png": "confirm",
            "pizzatron_start.png": "start",
        }
        for filename, expected in cases.items():
            frame = self._load_fixture(filename)
            for scale in (0.70, 1.30):
                with self.subTest(filename=filename, scale=scale):
                    resized = cv2.resize(frame, None, fx=scale, fy=scale)
                    self.assertEqual(bare_bot()._detect_restart_screen(resized), expected)

    def test_normal_pizzatron_gameplay_is_not_a_restart_screen(self) -> None:
        frame = cv2.imread(
            os.path.join(HERE, "test_fixtures", "normal_gameplay.png")
        )
        self.assertIsNotNone(frame)

        self.assertIsNone(bare_bot()._detect_restart_screen(frame))

    def test_blank_and_isolated_orange_patch_do_not_trigger_restart(self) -> None:
        blank = np.zeros((800, 1280, 3), dtype=np.uint8)
        isolated_cue = blank.copy()
        cv2.rectangle(
            isolated_cue,
            (int(0.05 * isolated_cue.shape[1]), int(0.86 * isolated_cue.shape[0])),
            (int(0.25 * isolated_cue.shape[1]), int(0.995 * isolated_cue.shape[0])),
            (0, 140, 230),
            thickness=-1,
        )

        bot = bare_bot()
        self.assertIsNone(bot._detect_restart_screen(blank))
        self.assertIsNone(bot._detect_restart_screen(isolated_cue))


class RestartFlowActionTests(unittest.TestCase):
    def test_relative_restart_click_uses_calibrated_game_origin_and_frame_size(self) -> None:
        bot = bare_bot()
        bot.cfg["game_top_left"] = [100, 50]
        frame = np.zeros((400, 800, 3), dtype=np.uint8)

        with (
            patch("pizza_bot.time.sleep"),
            patch("pizza_bot.pyautogui.moveTo") as move_to,
            patch("pizza_bot.pyautogui.click") as click,
        ):
            bot._click_game_relative((0.25, 0.50), frame)

        move_to.assert_called_once_with(300, 250, duration=0.08)
        click.assert_called_once_with()

    def test_restart_handler_requires_two_frames_and_debounces_retries(self) -> None:
        bot = bare_bot()
        bot.test_mode = False
        bot.status = "Running"
        bot._detect_restart_screen = lambda _frame: "score"
        bot._click_game_relative = Mock()
        frame = np.zeros((800, 1280, 3), dtype=np.uint8)

        self.assertTrue(bot._handle_restart_flow(frame, 10.00))
        bot._click_game_relative.assert_not_called()

        self.assertTrue(bot._handle_restart_flow(frame, 10.08))
        self.assertEqual(bot._click_game_relative.call_count, 1)
        point, clicked_frame = bot._click_game_relative.call_args.args
        self.assertEqual(point, bot.cfg["restart_done_rel"])
        self.assertIs(clicked_frame, frame)

        # Even two more matching frames must not repeat the click before the
        # configured action delay has elapsed.
        self.assertTrue(bot._handle_restart_flow(frame, 10.16))
        self.assertTrue(bot._handle_restart_flow(frame, 10.24))
        self.assertEqual(bot._click_game_relative.call_count, 1)

        self.assertTrue(bot._handle_restart_flow(frame, 11.09))
        self.assertEqual(bot._click_game_relative.call_count, 2)

    def test_kitchen_click_clears_tracking_and_uses_short_transition_delay(self) -> None:
        bot = bare_bot()
        bot.test_mode = False
        bot.status = "Running"
        visible_state = ["parlor"]
        bot._detect_restart_screen = lambda _frame: visible_state[0]
        bot._click_game_relative = Mock()
        bot._last_pizza_geometry = PizzaGeometry(
            900, 780, 212, 138, (0, 0, 0, 0), True
        )
        bot._last_pizza_geometry_at = 20.0
        bot._last_conveyor_speed = 123.0
        frame = np.zeros((800, 1280, 3), dtype=np.uint8)

        self.assertTrue(bot._handle_restart_flow(frame, 20.00))
        self.assertTrue(bot._handle_restart_flow(frame, 20.08))

        point, clicked_frame = bot._click_game_relative.call_args.args
        self.assertEqual(point, bot.cfg["restart_kitchen_rel"])
        self.assertIs(clicked_frame, frame)
        self.assertIsNone(bot._last_pizza_geometry)
        self.assertIsNone(bot._last_pizza_geometry_at)
        self.assertEqual(
            bot._last_conveyor_speed,
            float(bot.cfg["conveyor_speed_default"]),
        )
        self.assertAlmostEqual(
            bot._restart_next_action_at,
            20.08 + float(bot.cfg["restart_action_delay"]),
        )

        # Once the room disappears, suppress pizza actions during the short
        # dialog transition; the confirmation detector takes over after it.
        visible_state[0] = None
        self.assertTrue(bot._handle_restart_flow(frame, 20.50))
        self.assertFalse(bot._handle_restart_flow(frame, 21.09))
        self.assertEqual(bot.status, "Running")

    def test_restart_flow_clicks_kitchen_yes_and_play_in_order(self) -> None:
        bot = bare_bot()
        bot.test_mode = False
        bot.status = "Running"
        visible_state = ["parlor"]
        bot._detect_restart_screen = lambda _frame: visible_state[0]
        bot._click_game_relative = Mock()
        frame = np.zeros((800, 1280, 3), dtype=np.uint8)

        self.assertTrue(bot._handle_restart_flow(frame, 30.00))
        self.assertTrue(bot._handle_restart_flow(frame, 30.08))
        self.assertEqual(
            bot._click_game_relative.call_args_list[-1].args[0],
            bot.cfg["restart_kitchen_rel"],
        )

        visible_state[0] = "confirm"
        self.assertTrue(bot._handle_restart_flow(frame, 31.09))
        self.assertTrue(bot._handle_restart_flow(frame, 31.17))
        self.assertEqual(
            bot._click_game_relative.call_args_list[-1].args[0],
            bot.cfg["restart_confirm_yes_rel"],
        )

        visible_state[0] = "start"
        self.assertTrue(bot._handle_restart_flow(frame, 32.18))
        self.assertTrue(bot._handle_restart_flow(frame, 32.26))
        self.assertEqual(
            bot._click_game_relative.call_args_list[-1].args[0],
            bot.cfg["restart_play_rel"],
        )
        self.assertEqual(bot._click_game_relative.call_count, 3)
        self.assertAlmostEqual(
            bot._restart_guard_until,
            32.26 + float(bot.cfg["restart_load_delay"]),
        )

        visible_state[0] = None
        self.assertTrue(bot._handle_restart_flow(frame, 33.00))
        self.assertFalse(bot._handle_restart_flow(frame, 37.27))


class ActionPlanningTests(unittest.TestCase):
    def test_sauce_path_has_broad_spread_with_a_bounded_point_count(self) -> None:
        bot = bare_bot()
        radius_x, radius_y = 212, 138
        path = PizzaBot._build_sauce_path(
            radius_x,
            radius_y,
            fill_x=float(bot.cfg["sauce_fill_x_factor"]),
            fill_y=float(bot.cfg["sauce_fill_y_factor"]),
            edge_laps=int(bot.cfg["sauce_edge_laps"]),
            fill_points=int(bot.cfg["sauce_fill_points"]),
            edge_points=int(bot.cfg["sauce_edge_points"]),
        )
        xs = [point[0] for point in path]
        ys = [point[1] for point in path]

        # Keep the pass spatially useful without requiring near-perfect
        # simulated coverage.  The game tolerates overlap and does not need
        # every possible sauce stamp, while excess points cost real conveyor
        # time and can make the bot miss the next pizza.
        self.assertGreaterEqual(len(path), 36)
        self.assertLessEqual(len(path), 96)
        self.assertGreater(max(xs) - min(xs), radius_x * 1.50)
        self.assertGreater(max(ys) - min(ys), radius_y * 1.30)
        self.assertLessEqual(max(abs(x) for x in xs), radius_x)
        self.assertLessEqual(max(abs(y) for y in ys), radius_y)
        self.assertEqual(path[0], (0, 0))

    def test_default_sauce_pass_fits_the_next_pizza_time_budget(self) -> None:
        bot = bare_bot()
        radius_x, radius_y = 212, 138
        path = PizzaBot._build_sauce_path(
            radius_x,
            radius_y,
            fill_x=float(bot.cfg["sauce_fill_x_factor"]),
            fill_y=float(bot.cfg["sauce_fill_y_factor"]),
            edge_laps=int(bot.cfg["sauce_edge_laps"]),
            fill_points=int(bot.cfg["sauce_fill_points"]),
            edge_points=int(bot.cfg["sauce_edge_points"]),
        )

        sweep_seconds = len(path) * float(bot.cfg["sauce_motion_step_delay"])
        tracking_seconds = (
            max(0, int(bot.cfg["motion_sample_count"]) - 1)
            * float(bot.cfg["motion_sample_interval"])
        )
        fixed_seconds = (
            float(bot.cfg["sauce_pickup_delay"])
            + 0.055  # button-down registration
            + 0.10   # initial move to the dough
            + 0.06   # release/settle delay
        )

        self.assertLessEqual(sweep_seconds, 2.40)
        self.assertLessEqual(tracking_seconds + fixed_seconds + sweep_seconds, 2.90)

    def test_turbo_belt_shortens_sauce_path_and_sweep_time(self) -> None:
        geometry = PizzaGeometry(600, 780, 212, 138, (0, 0, 0, 0), True)

        def run_pass(speed: float) -> tuple[int, float]:
            bot = bare_bot()
            bot._measure_pizza_motion = lambda _x: (geometry, speed, 0.0)
            bot._set_cursor = Mock()
            bot.grab_game = lambda: np.zeros((798, 1283, 3), dtype=np.uint8)
            bot.detect_pizza_geometry = lambda _image: None
            bot._active_remaining_drops = 1

            with (
                patch("pizza_bot.time.sleep") as sleeper,
                patch("pizza_bot.time.perf_counter", return_value=0.0),
                patch("pizza_bot.pyautogui.moveTo"),
                patch("pizza_bot.pyautogui.mouseDown"),
                patch("pizza_bot.pyautogui.mouseUp"),
            ):
                self.assertTrue(bot.apply_sauce(False, geometry.center_x))

            step_delay = bot._speed_scaled(
                float(bot.cfg["sauce_motion_step_delay"]),
                float(bot.cfg.get("fast_sauce_motion_step_delay", 0.017)),
                speed,
            )
            step_sleeps = sum(
                1
                for call in sleeper.call_args_list
                if call.args and abs(float(call.args[0]) - step_delay) < 1e-9
            )
            self.assertEqual(step_sleeps, bot._set_cursor.call_count)
            return bot._set_cursor.call_count, step_sleeps * step_delay

        normal_points, normal_sweep = run_pass(90.0)
        turbo_points, turbo_sweep = run_pass(320.0)

        self.assertEqual(normal_points, 65)
        self.assertEqual(turbo_points, 41)
        self.assertLess(turbo_points, normal_points)
        self.assertLess(turbo_sweep, normal_sweep * 0.50)
        self.assertLessEqual(turbo_sweep, 0.70)

    def test_turbo_sauce_path_closes_the_sparse_outer_gap(self) -> None:
        bot = bare_bot()
        radius_x, radius_y = 212, 138
        fill_x = float(bot.cfg["sauce_fill_x_factor"])
        fill_y = float(bot.cfg["sauce_fill_y_factor"])
        path = np.asarray(
            PizzaBot._build_sauce_path(
                radius_x,
                radius_y,
                fill_x=fill_x,
                fill_y=fill_y,
                edge_laps=0,
                fill_points=int(bot.cfg["fast_sauce_fill_points"]),
                edge_points=int(bot.cfg["sauce_edge_points"]),
            ),
            dtype=float,
        )

        # Sample the whole planned fill ellipse, including its rim.  The old
        # 36-point turbo path left a roughly 40 px hole in an outer quadrant;
        # four additional points bring that worst gap below 38 px without a
        # perimeter lap or a materially longer sweep.
        work_rx = round(radius_x * fill_x)
        work_ry = round(radius_y * fill_y)
        candidates = np.asarray(
            [
                (
                    work_rx * radial * math.cos(angle),
                    work_ry * radial * math.sin(angle),
                )
                for radial in np.linspace(0.0, 1.0, 21)
                for angle in np.linspace(0.0, 2.0 * math.pi, 144, endpoint=False)
            ],
            dtype=float,
        )
        nearest = np.sqrt(
            ((candidates[:, None, :] - path[None, :, :]) ** 2).sum(axis=2)
        ).min(axis=1)

        self.assertLessEqual(float(nearest.max()), 38.0)

    def test_motion_speed_uses_multiple_frames_not_two_noisy_endpoints(self) -> None:
        bot = bare_bot()
        bot.cfg["motion_sample_count"] = 5
        bot.cfg["motion_sample_interval"] = 0
        centers = iter([600, 604, 609, 612, 616])

        def next_geometry(_fallback):
            return PizzaGeometry(next(centers), 780, 212, 138, (0, 0, 0, 0), True)

        bot._fresh_pizza_geometry = next_geometry
        with (
            patch("pizza_bot.time.sleep"),
            patch("pizza_bot.time.perf_counter", side_effect=[0.00, 0.04, 0.08, 0.12, 0.16]),
        ):
            geometry, speed, tracked_at = bot._measure_pizza_motion(600)

        self.assertEqual(geometry.center_x, 616)
        self.assertAlmostEqual(speed, 100.0, delta=8.0)
        self.assertEqual(tracked_at, 0.16)

    def test_motion_sampling_retains_last_fast_speed_when_slopes_are_invalid(self) -> None:
        bot = bare_bot()
        bot.cfg["motion_sample_count"] = 3
        bot.cfg["motion_sample_interval"] = 0
        bot._last_conveyor_speed = 300.0
        stationary = PizzaGeometry(700, 780, 212, 138, (0, 0, 0, 0), True)
        bot._fresh_pizza_geometry = lambda _fallback: stationary

        with (
            patch("pizza_bot.time.sleep"),
            patch("pizza_bot.time.perf_counter", side_effect=[0.00, 0.04, 0.08]),
        ):
            geometry, speed, tracked_at = bot._measure_pizza_motion(700)

        self.assertIs(geometry, stationary)
        self.assertEqual(speed, 300.0)
        self.assertEqual(tracked_at, 0.08)

    def test_make_pizza_dispatches_hot_sauce_and_five_shrimp(self) -> None:
        bot = bare_bot()
        calls = []
        bot.detect_order = lambda: DetectedOrder(
            Topping.HOT_SAUCE,
            {Topping.SHRIMP: 5},
        )
        bot.apply_sauce = lambda is_hot, x: calls.append(("sauce", is_hot, x))
        bot.apply_cheese = lambda x: calls.append(("cheese", x))
        bot.apply_topping_pieces = (
            lambda topping, x, count=5: calls.append(("extra", topping, x, count))
        )

        self.assertTrue(bot.make_pizza(620))
        self.assertEqual(
            calls,
            [
                ("sauce", True, 620),
                ("cheese", 620),
                ("extra", Topping.SHRIMP, 620, 5),
            ],
        )

    def test_make_pizza_stops_after_terminal_sauce_failure(self) -> None:
        bot = bare_bot()
        bot.detect_order = lambda: DetectedOrder(
            Topping.HOT_SAUCE,
            {Topping.SHRIMP: 5},
        )
        bot.apply_sauce = Mock(return_value=False)
        bot.apply_cheese = Mock(return_value=True)
        bot.apply_topping_pieces = Mock(return_value=True)

        self.assertFalse(bot.make_pizza(620))

        bot.apply_sauce.assert_called_once_with(True, 620)
        bot.apply_cheese.assert_not_called()
        bot.apply_topping_pieces.assert_not_called()
        self.assertTrue(bot._last_make_failure_terminal)
        self.assertEqual(bot.pizzas_made, 0)

    def test_hot_sauce_uses_hot_bottle_pickup(self) -> None:
        bot = bare_bot()
        geometry = PizzaGeometry(620, 780, 212, 138, (0, 0, 0, 0), True)
        bot._measure_pizza_motion = lambda _x: (geometry, 90.0, 0.0)
        bot._build_sauce_path = lambda _rx, _ry, **_kwargs: [(0, 0)]
        bot._set_cursor = lambda _x, _y: None
        bot.grab_game = lambda: np.zeros((798, 1283, 3), dtype=np.uint8)
        bot.detect_pizza_geometry = lambda _image: None

        with (
            patch("pizza_bot.time.sleep"),
            patch("pizza_bot.time.perf_counter", return_value=0.0),
            patch("pizza_bot.pyautogui.moveTo") as move_to,
            patch("pizza_bot.pyautogui.mouseDown"),
            patch("pizza_bot.pyautogui.mouseUp"),
        ):
            bot.apply_sauce(True, 620)

        self.assertEqual(move_to.call_args_list[0].args, tuple(bot.cfg["sauce_hot"]))

    def test_shrimp_pickup_uses_configured_source_and_registration_delays(self) -> None:
        bot = bare_bot()
        geometry = PizzaGeometry(620, 780, 212, 138, (0, 0, 0, 0), True)
        bot._fresh_pizza_geometry = lambda _x: geometry

        with (
            patch("pizza_bot.time.sleep") as sleep,
            patch("pizza_bot.pyautogui.moveTo") as move_to,
            patch("pizza_bot.pyautogui.mouseDown") as mouse_down,
            patch("pizza_bot.pyautogui.mouseUp") as mouse_up,
        ):
            bot.apply_topping_pieces(Topping.SHRIMP, 620, count=1)

        self.assertEqual(move_to.call_args_list[0].args, tuple(bot.cfg["topping_shrimp"]))
        mouse_down.assert_called_once()
        mouse_up.assert_called_once()
        delay_values = [call.args[0] for call in sleep.call_args_list]
        self.assertIn(bot.cfg["click_delay"], delay_values)
        self.assertIn(bot.cfg["post_topping_delay"], delay_values)


class MainLoopTimingTests(unittest.TestCase):
    def test_next_pizza_is_reacquired_within_half_a_second(self) -> None:
        bot = bare_bot()
        bot.test_mode = False
        bot.status = "Idle"
        geometry = PizzaGeometry(600, 780, 212, 138, (0, 0, 0, 0), True)
        frame = np.zeros((798, 1283, 3), dtype=np.uint8)
        clock = [100.0]
        made_at: list[float] = []

        bot.grab_game = lambda: frame
        bot.detect_pizza_geometry = lambda _image: geometry
        # A visible pizza in the target zone may already be the next order.
        # It must not force the scanner through a long fixed clear wait.
        bot.detect_pizza_x = lambda _image: geometry.center_x

        def make_pizza(_pizza_x: int) -> bool:
            made_at.append(clock[0])
            if len(made_at) == 2:
                bot.running = False
            return True

        def fake_sleep(seconds: float) -> None:
            clock[0] += seconds

        bot.make_pizza = make_pizza
        with (
            patch("pizza_bot.time.time", side_effect=lambda: clock[0]),
            patch("pizza_bot.time.sleep", side_effect=fake_sleep),
            patch("pizza_bot.time.perf_counter", side_effect=lambda: clock[0]),
        ):
            bot.run(setup_keyboard=False)

        self.assertEqual(len(made_at), 2)
        self.assertLessEqual(made_at[1] - made_at[0], 0.50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
