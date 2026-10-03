# Club Penguin Pizzatron 3000 Farmer

**ARE YOUR FLIPPERS TIRED FROM THE LATE-NIGHT PIZZA RUSH?** Then step right up and put down that squid! This free Windows automation tool tracks the conveyor as it accelerates, reads every recipe, serves the correct sauce and toppings, and automatically starts the next round. Fire it up and let your tireless digital chef keep those pizzas moving!

Under the hood, it uses local screen capture and OpenCV to locate a fully visible pizza, confirm each order across several frames, and keep every ingredient locked to the moving dough. Processing stays on your computer—no cloud service or general-purpose OCR is required.

Project by **[Raising Kaines](https://github.com/raisingkaines)**. Creator/contact on Discord: **@salemcorpse**.

This is an unofficial fan project and is not affiliated with Disney or Club Penguin. Use automation only where it is permitted.

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.10+ | Required when running from source |
| Windows | Mouse control and hotkeys are Windows-focused |
| Club Penguin in a browser | Keep the entire game visible and unobstructed |
| Stable browser size and zoom | Recalibrate after moving or resizing the game |

Install the source dependencies with:

```powershell
git clone https://github.com/raisingkaines/Club-Penguin-Pizzatron-3000-Farmer.git
cd Club-Penguin-Pizzatron-3000-Farmer
py -m pip install -r requirements.txt
```

## Quick start

Want the ready-to-run package? Grab the latest Windows build from the [Releases page](https://github.com/raisingkaines/Club-Penguin-Pizzatron-3000-Farmer/releases/latest). Prefer to inspect every ingredient yourself? The complete Python source and build script are included in this repository.

### 1. Calibrate

Use **Calibrate** in the GUI, or run:

```powershell
python calibrate.py
```

Calibration records the game and order-board bounds, topping bins, conveyor, and target position. Each sauce bottle has two distinct calibration points:

1. **Bottle body / pickup point** — a reliable point where the game lets the bot grab the bottle.
2. **Dispensing nozzle tip** — the exact point from which sauce appears while the bottle is held.

Do this separately for the regular and hot bottles. The saved `sauce_regular_nozzle_offset` and `sauce_hot_nozzle_offset` values are calculated from the corresponding pickup point to the nozzle tip. Re-run calibration if an older `config.json` does not contain these values; dimension-based fallbacks exist, but direct calibration is more accurate.

### 2. Test detection without clicks

In the GUI, use **Test Detection**. From the command line, use test and debug modes together:

```powershell
python pizza_bot.py --test --debug
```

Press **F2** to start the command-line test. Test mode reads pizzas and orders but does not apply ingredients. Confirm that the log reports the correct sauce, extras, piece counts, confidence, and frame votes before using active mode.

### 3. Run the bot

Choose one of:

```text
Launch Bot.bat
Pizzatron3000Bot-salemcorpse-fast.exe
python gui.py
python pizza_bot.py
```

The GUI provides Start, Pause, and Stop controls. For direct command-line use:

| Control | Action |
|---|---|
| **F2** | Start or pause |
| **Esc** | Stop |
| Move the cursor to the top-left corner | PyAutoGUI emergency abort |

## How detection works

### Full-pizza gate

The conveyor detector measures the pizza contour, bounding box, and expected radii. It does not begin work while the dough is clipped by either edge of the game. A pizza must:

- be fully visible;
- have enough detected width to represent the crust rather than a small topping; and
- enter a safe target zone at least one pizza radius inside the game.

This prevents the bot from starting the sauce pass while part of the dough is still off-screen. In conveyor debug images, a fully visible contour is green and a partial contour is orange.

### Stable order recognition

Order recognition uses the navy ingredient lines on the board as its primary signal:

1. It extracts and scale-normalizes the line widths.
2. It compares each line to the width of the first `CHEESE` line, so browser resolution changes do not directly change the classification.
3. It uses preview colors as secondary evidence when resolving extra toppings.
4. By default it captures three board frames and requires a majority-stable recipe. `order_samples` can be set from 1 to 5.

Regular and hot sauce are classified from the sauce-line-to-cheese width ratio. There is an intentional uncertainty band between the two classes. An out-of-range or ambiguous sauce line raises an order-reading error; the bot safely skips that pizza instead of silently choosing regular sauce. Debug mode logs normalized widths, recognition confidence, and the winning vote count.

Quantity-prefixed `SQUID` can overlap the width of a bare `SEAWEED` row. In that overlap, the bot now uses proportional blue-versus-green evidence from the order preview. If the preview is still loading or the colours are inconclusive, it retries or skips the order instead of placing the wrong topping.

### Exact extra-piece counts

The number of different extra ingredients controls how many pieces are placed:

| Different extras requested | Pieces placed |
|---:|---|
| 0 | Sauce and cheese only |
| 1 | 5 pieces of that extra |
| 2 | 2 pieces of each extra |
| 4 | 1 piece of each extra |

An unsupported number of extra ingredient lines is treated as an unreadable order and skipped safely. The supported extras are seaweed, shrimp, squid, and fish.

### Sauce application

Once the full-pizza gate passes, the bot:

- measures current conveyor motion instead of relying only on a fixed speed;
- progressively shortens its sauce sweep and pickup/drop delays as the belt accelerates;
- grabs the requested bottle at its calibrated pickup point;
- offsets the cursor so the calibrated nozzle tip, not the bottle body, follows the pizza;
- paints a compact, evenly distributed fill while continuously correcting for conveyor movement;
- keeps later cheese and extra pieces locked to the same tracked pizza if a new one appears; and
- estimates warm-color coverage afterward.

At turbo speed the fill uses 40 distributed interior samples at 17 ms each. This closes the small sparse outer gap while adding only about 31 ms compared with the previous fast pass. The bot does not perform a full second pickup-and-sweep because that extra delay could make it miss the remaining cheese or toppings; it logs a warning if the measured coverage is still low.

Before beginning a fast-belt sauce pass, the bot estimates how much conveyor time remains and reserves time for cheese plus every requested extra. If a complete recipe no longer fits, it skips that pizza without clicking an ingredient. During cheese and topping placement, the tracked pizza is projected forward; once it reaches the right safety boundary, all remaining drops are cancelled. A rejected next-pizza contour can no longer fall back to the old left-side work position, which prevents one pizza's recipe from spilling onto the next pizza.

### Automatic round restart

With `auto_restart` enabled (the default), one Start press covers consecutive rounds. The bot recognizes the end-of-round score screen, clicks **Done**, closes the blue reward/stamp popup, enters the **Kitchen**, clicks **Yes** in the play confirmation, and clicks **Play** on the Pizzatron start screen.

Restart recognition uses several large color regions from each screen and requires two matching frames before clicking. Reward detection runs before score detection because the reward modal appears over the score screen. Normal pizza processing is suspended during these screens and for the configured loading grace period, preventing room or transition artwork from being mistaken for a conveyor pizza.

## Debug and test workflow

Use this sequence after calibration or any browser/server change:

1. Run `python pizza_bot.py --test --debug` and press **F2**.
2. Verify that each order is stable and that hot/regular sauce and shrimp are reported correctly.
3. Inspect `debug_frames/`:
   - `conv_*.png` shows the selected conveyor contour and full-pizza gate.
   - `board_*.png` shows the captured order board.
   - `board_text_*.png` shows the extracted navy ingredient text.
4. Run `python pizza_bot.py --debug` for a supervised active test.
5. Watch the log for the sauce coverage estimate or a nozzle-calibration warning.

Do not diagnose recognition by immediately switching to active mode. A detection-only run avoids unwanted clicks while the crop and calibration are being checked.

## Configuration

Calibration writes `config.json`. The most important position fields are:

| Key | Meaning |
|---|---|
| `game_top_left`, `game_bottom_right` | Full game capture bounds |
| `order_top_left`, `order_bottom_right` | White order-board bounds |
| `sauce_regular`, `sauce_hot` | Bottle-body pickup points |
| `sauce_regular_nozzle_offset` | Regular nozzle tip minus regular pickup point |
| `sauce_hot_nozzle_offset` | Hot nozzle tip minus hot pickup point |
| `topping_*` | Center/pickup point for each topping bin |
| `conveyor_y` | Vertical center of the pizza path |
| `target_x` | Preferred horizontal work position |

Positive nozzle offsets point right and down from the pickup point. Prefer re-running calibration over guessing these values.

Useful optional tuning fields and their runtime defaults are:

| Key | Default | Purpose |
|---|---:|---|
| `scan_interval` | `0.05` | Seconds between conveyor scans |
| `aborted_pizza_cooldown` | `0.45` | Brief quarantine after losing a pizza so it is not started again |
| `fast_conveyor_speed` | `130.0` | Belt speed where adaptive shortening begins |
| `turbo_conveyor_speed` | `320.0` | Belt speed where fast timings are fully applied |
| `fast_click_delay` | `0.04` | Ingredient pickup hold at turbo speed |
| `fast_post_topping_delay` | `0.03` | Post-drop settle time at turbo speed |
| `topping_drop_lead_seconds` | `0.10` | Forward prediction through pickup-to-release latency |
| `topping_right_margin_factor` | `0.42` | Right-edge safety margin relative to pizza radius |
| `auto_restart` | `true` | Continue through score, reward, parlor, confirmation, and start screens |
| `restart_action_delay` | `1.0` | Minimum seconds between restart-navigation clicks |
| `restart_load_delay` | `5.0` | Gameplay-inhibit period after clicking Play |
| `restart_done_rel` | `[0.149, 0.944]` | Done-button point as a fraction of the game rectangle |
| `restart_reward_close_rel` | `[0.701, 0.134]` | Reward-popup close point as a fraction of the game rectangle |
| `restart_kitchen_rel` | `[0.264, 0.248]` | Kitchen-entrance point as a fraction of the game rectangle |
| `restart_confirm_yes_rel` | `[0.428, 0.459]` | Yes-button point as a fraction of the game rectangle |
| `restart_play_rel` | `[0.160, 0.812]` | Pizzatron Play-button point as a fraction of the game rectangle |
| `target_zone_radius` | `130` | Width of the valid work zone after the safe target |
| `pizza_edge_margin` | `12` | Additional edge clearance before work begins |
| `order_samples` | `3` | Board samples used for stable recognition; clamped to 1–5 |
| `order_sample_interval` | `0.025` | Seconds between order samples |
| `order_min_confidence` | `0.5` | Minimum accepted recognition confidence |
| `order_retry_window` | `0.45` | Bounded retry time for a transitioning order card |
| `motion_sample_interval` | `0.04` | Interval used to measure conveyor speed |
| `motion_sample_count` | `5` | Frames used for robust conveyor-speed measurement |
| `conveyor_speed_default` | `90.0` | Fallback pixels/second if motion measurement is unreliable |
| `sauce_pickup_delay` | `0.075` | Pause before pressing the bottle |
| `sauce_motion_step_delay` | `0.029` | Dwell per distributed sauce position so the client registers it |
| `fast_sauce_motion_step_delay` | `0.017` | Sauce-position dwell at turbo belt speed |
| `sauce_fill_x_factor` | `0.84` | Horizontal fill radius relative to the detected crust |
| `sauce_fill_y_factor` | `0.73` | Vertical fill radius relative to the detected crust |
| `sauce_edge_laps` | `0` | Optional perimeter laps; disabled to keep pace with new pizzas |
| `sauce_fill_points` | `64` | Evenly distributed interior sauce positions |
| `fast_sauce_fill_points` | `40` | Planned interior positions at turbo speed |
| `minimum_sauce_fill_points` | `24` | Minimum allowed when the remaining-time budget trims a pass |
| `sauce_edge_points` | `20` | Positions used for each optional perimeter lap |
| `sauce_reanchor_interval` | `0.40` | Seconds between live pizza-position corrections |
| `fast_sauce_reanchor_interval` | `0.22` | Reanchor interval at turbo belt speed |
| `sauce_center_offset_x` | `3` | Horizontal sauce-center correction inside the crust |
| `sauce_center_offset_y` | `-6` | Vertical correction for the slightly high inner dough |
| `sauce_coverage_warn_threshold` | `0.55` | Coverage estimate below which a warning is logged |
| `dough_hsv_lower` | `[10, 20, 155]` | Lower HSV bound for pizza detection |
| `dough_hsv_upper` | `[40, 170, 255]` | Upper HSV bound for pizza detection |
| `pizza_min_area` | `600` | Minimum contour area considered a pizza |
| `conveyor_strip_half_height` | `75` | Half-height of the conveyor scan strip |

Change timing values cautiously. Faster cursor events are not necessarily registered by every browser or game client.

## Troubleshooting

| Problem | What to check |
|---|---|
| Bot waits while a pizza is visible | Inspect `conv_*.png`; the pizza may still be partial, too narrow, or outside the safe target zone. Recheck game bounds and conveyor calibration. |
| Sauce covers only a small area | Recalibrate the bottle **body** and **nozzle tip** as separate points. Run active debug mode and check the coverage warning. |
| Regular sauce is chosen for a hot order | Use test/debug mode and inspect `board_text_*.png`. Tighten the order-board crop; ambiguous frames should be skipped, not defaulted. |
| Shrimp or another extra is missing/wrong | Check the detected line widths, piece counts, and preview crop in test/debug mode. Keep the complete white board visible. |
| A round does not restart | Keep the full game visible and unobstructed. Confirm `auto_restart` is `true`; if the transition is slow, raise `restart_load_delay`. |
| Restart clicks miss their buttons | Recalibrate the game rectangle first. The five `restart_*_rel` points are normalized to that rectangle and can be fine-tuned if the server uses a different layout. |
| Clicks land in the wrong place | Recalibrate at the same browser zoom, display scaling, and window position used while playing. |
| Pizza is not detected | Inspect conveyor debug images, then adjust the dough HSV bounds or `pizza_min_area` if necessary. |
| Late-round ingredients land on the next pizza | Use this fast-belt build and keep the complete conveyor visible. It cancels a recipe when the original pizza reaches the right safety boundary instead of falling back to the next pizza. |
| `config.json` is missing or from an older build | Run the current calibration wizard again, especially to add nozzle offsets. |

## Project structure

```text
Club-Penguin-Pizzatron-3000-Farmer/
├── assets/                    # GUI background and icon
├── test_fixtures/             # Regression screenshots used by the tests
├── .gitignore                 # Generated/local files excluded from Git
├── Launch Bot.bat             # Windows source launcher
├── Pizzatron3000Bot.spec      # Portable PyInstaller configuration
├── build.py                   # PyInstaller build helper
├── calibrate.py               # Command-line calibration
├── gui.py                     # Desktop GUI and calibration wizard
├── pizza_bot.py               # Detection and automation engine
├── config.example.json        # Example calibration structure
├── requirements.txt           # Python dependencies
├── test_pizza_bot.py          # Automated regression suite
└── README.md
```

## Safety and acceptable use

This project is for educational and personal experimentation. Automation may violate the rules of a Club Penguin server and could result in penalties or account loss. Use it only where you have permission and accept responsibility for complying with the applicable rules.

The program controls the real mouse cursor and can click outside the game if calibration, display scaling, or window placement changes. Test without clicks first, keep the game unobstructed, remain present while it runs, and keep the GUI Stop control, **Esc**, or the PyAutoGUI top-left failsafe available. Do not use it around sensitive applications or unattended workflows.
