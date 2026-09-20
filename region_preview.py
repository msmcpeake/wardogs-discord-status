"""Shows a screenshot of a monitor with the configured capture regions drawn
on top, so you can check they line up with the game's UI on your resolution.

    python region_preview.py

Also reachable from the tray icon's "Show capture regions..." menu item.
Read-only: edit CAPTURE_REGION / TEAM_ICON_REGION / MONITOR_INDEX in .env,
hit Refresh here to see the new boxes, then restart the app to apply them.
"""

import os
import tkinter as tk
from tkinter import ttk

import mss
from dotenv import dotenv_values
from PIL import Image, ImageDraw, ImageFont, ImageTk

import wardogs_status_bot as bot

ENV_PATH = os.path.join(bot.SCRIPT_DIR, ".env")

# (.env key, label drawn on the box, box color, default value)
REGION_SPECS = [
    ("CAPTURE_REGION", "OCR capture region", "#ffd000", bot.DEFAULT_CAPTURE_REGION),
    ("TEAM_ICON_REGION", "Team icon region", "#ff3b3b", bot.DEFAULT_TEAM_ICON_REGION),
]


def read_config():
    """Current values straight from .env (re-read on every refresh, so edits
    show up without reopening this window). Returns (regions, monitor_index)
    where regions maps key -> raw string."""
    file_values = dotenv_values(ENV_PATH)
    regions = {key: (file_values.get(key) or default).strip() for key, _l, _c, default in REGION_SPECS}
    try:
        monitor_index = int(file_values.get("MONITOR_INDEX") or bot.DEFAULT_MONITOR_INDEX)
    except ValueError:
        monitor_index = bot.DEFAULT_MONITOR_INDEX
    return regions, monitor_index


def list_monitors():
    """[(mss index, width, height, is_primary)] for every real monitor."""
    with mss.MSS() as sct:
        return [(i, m["width"], m["height"], bool(m.get("is_primary"))) for i, m in enumerate(sct.monitors) if i >= 1]


def grab_monitor(index: int) -> Image.Image:
    with mss.MSS() as sct:
        shot = sct.grab(sct.monitors[index])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _load_font(size: int):
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def region_geometry(region_str: str, width: int, height: int):
    """(x, y, w, h) in real pixels, computed exactly the way grab_region does."""
    left, top, right, bottom = bot.parse_region(region_str)
    return int(width * left), int(height * top), int(width * (right - left)), int(height * (bottom - top))


def render_preview(screenshot: Image.Image, regions: dict, max_size):
    """Scales the screenshot down to fit max_size and draws each configured
    region on it. Returns (preview_image, {key: error_message}) - a region
    that fails to parse is skipped and reported instead of crashing."""
    scale = min(max_size[0] / screenshot.width, max_size[1] / screenshot.height, 1.0)
    pw, ph = max(1, int(screenshot.width * scale)), max(1, int(screenshot.height * scale))
    # Base stays RGB: ImageDraw's "RGBA" mode blends translucent shapes onto an
    # RGB image, but would overwrite pixels (making the boxes solid) on an
    # RGBA one.
    preview = screenshot.resize((pw, ph), Image.LANCZOS).convert("RGB")
    overlay = ImageDraw.Draw(preview, "RGBA")
    font = _load_font(15)
    errors = {}

    for key, label, color, _default in REGION_SPECS:
        try:
            left, top, right, bottom = bot.parse_region(regions[key])
        except ValueError as e:
            errors[key] = str(e)
            continue
        x0, y0, x1, y1 = left * pw, top * ph, right * pw, bottom * ph
        overlay.rectangle([x0, y0, x1, y1], fill=color + "28", outline=color, width=3)

        text = f"{label}"
        tb = overlay.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        # Sit the label just above the box (or just below if there's no room
        # above), nudged left if it would run off the right edge.
        lx = max(4, min(x0, pw - tw - 10))
        ly = y0 - th - 10 if y0 - th - 10 >= 0 else y1 + 4
        overlay.rectangle([lx - 3, ly - 2, lx + tw + 5, ly + th + 6], fill=(0, 0, 0, 200))
        overlay.text((lx, ly), text, font=font, fill=color)

    return preview, errors


class PreviewApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Wardogs status - capture regions")
        self.monitors = list_monitors()
        _regions, configured = read_config()
        # Preview the monitor the app is actually configured to use, if it exists.
        self.selected_index = configured if any(m[0] == configured for m in self.monitors) else self.monitors[0][0]

        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Monitor:").pack(side="left")
        self.combo = ttk.Combobox(top, state="readonly", width=46)
        self.combo.pack(side="left", padx=(4, 8))
        self.combo.bind("<<ComboboxSelected>>", self._on_monitor_picked)
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(top, text="Capture in 5s (switch to the game)", command=lambda: self.refresh(5000)).pack(
            side="left", padx=6
        )
        self._set_combo_values(configured)

        self.image_label = ttk.Label(root)
        self.image_label.pack(padx=6, pady=(0, 4))
        self.info = tk.Text(root, height=8, font=("Consolas", 10), wrap="word", relief="flat", background="#f3f3f3")
        self.info.pack(fill="x", padx=6, pady=(0, 6))
        self.info.configure(state="disabled")

        self._photo = None
        self.refresh()

    def _set_combo_values(self, configured_index: int):
        labels = []
        for index, w, h, primary in self.monitors:
            label = f"Monitor {index} - {w}x{h}" + (" (primary)" if primary else "")
            if index == configured_index:
                label += "  <- used by the app"
            labels.append(label)
        self.combo["values"] = labels
        self.combo.current([m[0] for m in self.monitors].index(self.selected_index))

    def _on_monitor_picked(self, _event):
        self.selected_index = self.monitors[self.combo.current()][0]
        self.refresh()

    def refresh(self, delay_ms: int = 0):
        """Hides this window while capturing so it isn't in its own screenshot
        (the delay variant leaves time to alt-tab to the game first)."""
        self.root.withdraw()
        self.root.after(delay_ms or 250, self._capture_and_show)

    def _capture_and_show(self):
        try:
            regions, configured = read_config()
            self._set_combo_values(configured)
            screenshot = grab_monitor(self.selected_index)
            max_size = (int(self.root.winfo_screenwidth() * 0.85), int(self.root.winfo_screenheight() * 0.62))
            preview, errors = render_preview(screenshot, regions, max_size)
            self._photo = ImageTk.PhotoImage(preview)
            self.image_label.configure(image=self._photo)
            self._write_info(screenshot.size, regions, configured, errors)
        except Exception as e:  # a bad capture shouldn't leave an invisible window
            self._write_text(f"Couldn't capture monitor {self.selected_index}: {e}")
        finally:
            self.root.deiconify()

    def _write_info(self, size, regions, configured, errors):
        width, height = size
        lines = [f"Monitor {self.selected_index}: {width}x{height} px"]
        if self.selected_index == configured:
            lines[0] += "  (the monitor the app reads - MONITOR_INDEX in .env)"
        else:
            lines[0] += f"  (the app reads monitor {configured} - set MONITOR_INDEX={self.selected_index} in .env to use this one)"
        lines.append("")
        for key, label, _color, _default in REGION_SPECS:
            if key in errors:
                lines.append(f"{label}: {key} is invalid - {errors[key]}")
                continue
            x, y, w, h = region_geometry(regions[key], width, height)
            lines.append(f"{label}: {key}={regions[key]}  ->  {w} x {h} px at ({x}, {y})")
        lines.append("")
        lines.append(
            "Read-only. Edit these in .env and hit Refresh to preview, then restart the app "
            "(tray icon -> Quit, then relaunch) to apply them."
        )
        self._write_text("\n".join(lines))

    def _write_text(self, text: str):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", text)
        self.info.configure(state="disabled")


def main():
    root = tk.Tk()
    PreviewApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
