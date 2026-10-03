"""
Build helper — generates icon.ico from background art,
then runs PyInstaller to produce Pizzatron3000Bot.exe.

Usage:
    python build.py
"""

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BG_PATH = os.path.join(SCRIPT_DIR, "assets", "background.png")
ICON_PATH = os.path.join(SCRIPT_DIR, "assets", "icon.ico")


def make_icon():
    """Create a .ico from the background art."""
    from PIL import Image

    if not os.path.exists(BG_PATH):
        print("[!] assets/background.png not found -- skipping icon.")
        return None

    img = Image.open(BG_PATH).convert("RGBA")
    # Crop to square (center)
    side = min(img.width, img.height)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))

    # Generate multiple icon sizes
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    icons = [img.resize(s, Image.Resampling.LANCZOS) for s in sizes]
    icons[0].save(ICON_PATH, format="ICO", sizes=sizes, append_images=icons[1:])
    print(f"[OK] Icon saved -> {ICON_PATH}")
    return ICON_PATH


def build_exe(icon_path: str | None):
    """Run PyInstaller to create a single .exe."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "Pizzatron3000Bot",
        "--add-data", f"assets{os.pathsep}assets",
        "--hidden-import", "pizza_bot",
    ]

    if icon_path and os.path.exists(icon_path):
        cmd += ["--icon", icon_path]

    # Include pizza_bot.py as data so the frozen exe can import it
    cmd += ["--add-data", f"pizza_bot.py{os.pathsep}."]

    cmd.append("gui.py")

    print(f"\n[*] Building .exe ...\n")
    print(f"    {' '.join(cmd)}\n")
    subprocess.check_call(cmd, cwd=SCRIPT_DIR)

    exe_path = os.path.join(SCRIPT_DIR, "dist", "Pizzatron3000Bot.exe")
    print(f"\n{'='*52}")
    print(f"  [OK] Build complete!")
    print(f"  Output: {exe_path}")
    print(f"{'='*52}\n")


def main():
    # 1. Install PyInstaller if needed
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[*] Installing PyInstaller ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # 2. Generate icon
    icon = make_icon()

    # 3. Build
    build_exe(icon)


if __name__ == "__main__":
    main()
