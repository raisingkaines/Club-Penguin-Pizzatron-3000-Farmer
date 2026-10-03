"""
Pizzatron 3000 Bot - Calibration Tool
======================================
Run this FIRST to record the positions of all game elements on your screen.

Usage:
    python calibrate.py

For each prompt, move your mouse to the specified position and press ENTER.
Positions are saved to config.json for the bot to use.
"""

import json
import os
import sys
import time

import mss
import numpy as np
import cv2
import pyautogui


CONFIG_FILE = "config.json"


def record_position(prompt: str) -> list[int]:
    """Prompt user to position their mouse, then record coordinates."""
    print(f"\n  → {prompt}")
    input("    Move your mouse there and press ENTER... ")
    pos = pyautogui.position()
    print(f"    ✓ Recorded ({pos.x}, {pos.y})")
    return [pos.x, pos.y]


def main():
    print("=" * 58)
    print("   🍕  PIZZATRON 3000 BOT — CALIBRATION")
    print("=" * 58)
    print()
    print("  Make sure the Pizzatron 3000 game is fully visible")
    print("  on your screen before starting!")
    print()
    print("  For each step you will move your mouse to the")
    print("  requested position and press ENTER to record it.")
    print()
    input("  Press ENTER to begin calibration... ")

    config: dict = {}

    # ── Game area boundaries ────────────────────────────────────
    print("\n┌─ GAME AREA ─────────────────────────────────────────┐")
    config["game_top_left"] = record_position(
        "TOP-LEFT corner of the game area (where the wall meets the edge)"
    )
    config["game_bottom_right"] = record_position(
        "BOTTOM-RIGHT corner of the game area (end of conveyor belt)"
    )

    # ── Order board ─────────────────────────────────────────────
    print("\n┌─ ORDER BOARD ───────────────────────────────────────┐")
    config["order_top_left"] = record_position(
        "TOP-LEFT corner of the white ORDER BOARD rectangle"
    )
    config["order_bottom_right"] = record_position(
        "BOTTOM-RIGHT corner of the white ORDER BOARD rectangle"
    )

    # ── Sauce dispensers ────────────────────────────────────────
    print("\n┌─ SAUCE BOTTLES ─────────────────────────────────────┐")
    regular_pickup = record_position(
        "REGULAR SAUCE bottle BODY / pickup point (orange, left-most)"
    )
    regular_nozzle = record_position(
        "TIP of the REGULAR SAUCE dispensing nozzle"
    )
    hot_pickup = record_position(
        "HOT SAUCE bottle BODY / pickup point (red, says 'HOT')"
    )
    hot_nozzle = record_position(
        "TIP of the HOT SAUCE dispensing nozzle"
    )
    config["sauce_regular"] = regular_pickup
    config["sauce_hot"] = hot_pickup
    config["sauce_regular_nozzle_offset"] = [
        regular_nozzle[0] - regular_pickup[0],
        regular_nozzle[1] - regular_pickup[1],
    ]
    config["sauce_hot_nozzle_offset"] = [
        hot_nozzle[0] - hot_pickup[0],
        hot_nozzle[1] - hot_pickup[1],
    ]

    # ── Topping bins (left → right) ────────────────────────────
    print("\n┌─ TOPPING BINS (click center of each, left to right) ┐")
    config["topping_cheese"] = record_position("CHEESE bin (shredded yellow)")
    config["topping_seaweed"] = record_position("SEAWEED bin (green)")
    config["topping_shrimp"] = record_position("SHRIMP bin (pink)")
    config["topping_squid"] = record_position("SQUID bin (blue)")
    config["topping_fish"] = record_position("FISH bin (gray / silver)")

    # ── Conveyor belt & target zone ─────────────────────────────
    print("\n┌─ CONVEYOR BELT & TARGET ZONE ───────────────────────┐")
    conveyor_pos = record_position(
        "CENTER of the CONVEYOR BELT (vertical center where pizzas travel)"
    )
    config["conveyor_y"] = conveyor_pos[1]

    target_pos = record_position(
        "IDEAL DROP POINT — where you'd click to place toppings on a pizza\n"
        "    (roughly below the topping bins, center-ish on the conveyor)"
    )
    config["target_x"] = target_pos[0]

    # ── Timing defaults (editable in config.json) ───────────────
    config["click_delay"] = 0.06          # hold long enough for source pickup
    config["post_topping_delay"] = 0.06   # let the drop register before the next piece
    config["fast_click_delay"] = 0.04
    config["fast_post_topping_delay"] = 0.03
    config["scan_interval"] = 0.05        # seconds between screen scans
    config["post_pizza_cooldown"] = 0.2   # extra delay after the completed pizza clears
    config["aborted_pizza_cooldown"] = 0.45
    config["auto_restart"] = True
    config["restart_action_delay"] = 1.0
    config["restart_load_delay"] = 5.0
    config["restart_done_rel"] = [0.149, 0.944]
    config["restart_reward_close_rel"] = [0.701, 0.134]
    config["restart_kitchen_rel"] = [0.264, 0.248]
    config["restart_confirm_yes_rel"] = [0.428, 0.459]
    config["restart_play_rel"] = [0.160, 0.812]
    config["target_zone_radius"] = 130    # px — pizza must be within this of target_x
    config["pizza_edge_margin"] = 12
    game_width = config["game_bottom_right"][0] - config["game_top_left"][0]
    game_height = config["game_bottom_right"][1] - config["game_top_left"][1]
    config["pizza_radius_x"] = round(game_width * 0.165)
    config["pizza_radius_y"] = round(game_height * 0.173)
    config["pizza_tracking_cache_max_age"] = 6.0
    config["fast_conveyor_speed"] = 130.0
    config["turbo_conveyor_speed"] = 320.0
    config["topping_drop_lead_seconds"] = 0.10
    config["topping_right_margin_factor"] = 0.42
    config["order_samples"] = 3
    config["order_sample_interval"] = 0.025
    config["order_min_confidence"] = 0.5
    config["order_retry_window"] = 0.45
    config["motion_sample_interval"] = 0.04
    config["motion_sample_count"] = 5
    config["conveyor_speed_default"] = 90.0
    config["sauce_motion_step_delay"] = 0.029
    config["fast_sauce_motion_step_delay"] = 0.017
    config["sauce_fill_x_factor"] = 0.84
    config["sauce_fill_y_factor"] = 0.73
    config["sauce_edge_laps"] = 0
    config["sauce_fill_points"] = 64
    config["fast_sauce_fill_points"] = 40
    config["minimum_sauce_fill_points"] = 24
    config["sauce_edge_points"] = 20
    config["sauce_reanchor_interval"] = 0.40
    config["fast_sauce_reanchor_interval"] = 0.22
    config["sauce_center_offset_x"] = 3
    config["sauce_center_offset_y"] = -6
    config["sauce_pickup_delay"] = 0.075
    config["fast_sauce_pickup_delay"] = 0.05
    config["sauce_right_margin_factor"] = 0.84
    config["estimated_drop_seconds"] = 0.30
    config["fast_estimated_drop_seconds"] = 0.24
    config["fast_action_reserve"] = 0.18
    config["sauce_coverage_warn_threshold"] = 0.55

    # ── Detection tuning (HSV ranges, editable) ─────────────────
    config["dough_hsv_lower"] = [10, 20, 155]
    config["dough_hsv_upper"] = [40, 170, 255]
    config["pizza_min_area"] = 600
    config["conveyor_strip_half_height"] = 75

    # ── Save ────────────────────────────────────────────────────
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    # Save a reference screenshot of the game area
    sct = mss.mss()
    tl = config["game_top_left"]
    br = config["game_bottom_right"]
    monitor = {
        "left": tl[0], "top": tl[1],
        "width": br[0] - tl[0], "height": br[1] - tl[1],
    }
    frame = np.array(sct.grab(monitor))[:, :, :3]
    cv2.imwrite("calibration_screenshot.png", frame)

    print(f"\n{'=' * 58}")
    print(f"  ✅  Calibration saved to {CONFIG_FILE}")
    print(f"  📸  Reference screenshot → calibration_screenshot.png")
    print(f"\n  Next step:  python pizza_bot.py")
    print(f"{'=' * 58}\n")


if __name__ == "__main__":
    main()
