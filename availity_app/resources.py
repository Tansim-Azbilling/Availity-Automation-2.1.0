"""Bundled assets (PyInstaller vs source directory)."""
import os
import sys

import tkinter as tk

def _resource_path(filename):
    """Resolve a bundled-resource path that works in both .py and PyInstaller .exe modes.

    PyInstaller unpacks data files into ``sys._MEIPASS`` at runtime; running
    directly from source we fall back to the script's directory.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


def _load_brand_logo(path, target_height=56):
    """Load and resize the brand logo for the header.

    Pillow is preferred for high-quality resampling. When Pillow is not
    available we fall back to tk.PhotoImage and integer ``subsample`` —
    PNG support is built into Tk 8.6+. Returns None (and logs nothing)
    if the file is missing so the GUI still renders without the logo.
    """
    if not os.path.exists(path):
        return None

    try:
        from PIL import Image, ImageTk  # type: ignore
        img = Image.open(path)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        scale = target_height / float(img.height)
        new_size = (max(1, int(img.width * scale)), target_height)
        img = img.resize(new_size, Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:
        pass

    try:
        photo = tk.PhotoImage(file=path)
        if photo.height() > target_height:
            factor = max(1, photo.height() // target_height)
            photo = photo.subsample(factor, factor)
        return photo
    except Exception:
        return None


def _make_settings_icon(size=16, color="#6B7280"):
    """Draw a settings cog PhotoImage so the icon is not font/emoji dependent.

    The Unicode GEAR glyph (U+2699) often renders as a flower-like dingbat
    in Segoe UI. Returns None if Pillow is unavailable.
    """
    try:
        from math import cos, pi, sin

        from PIL import Image, ImageDraw, ImageTk  # type: ignore
    except Exception:
        return None

    scale = 4
    canvas = size * scale
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = canvas / 2.0
    teeth = 8
    outer_r = canvas * 0.47
    inner_r = canvas * 0.32
    hole_r = canvas * 0.13
    tooth_half = (pi / teeth) * 0.55

    points = []
    for i in range(teeth):
        a = 2 * pi * i / teeth
        points.append((cx + inner_r * cos(a - pi / teeth), cy + inner_r * sin(a - pi / teeth)))
        points.append((cx + outer_r * cos(a - tooth_half), cy + outer_r * sin(a - tooth_half)))
        points.append((cx + outer_r * cos(a + tooth_half), cy + outer_r * sin(a + tooth_half)))
        points.append((cx + inner_r * cos(a + pi / teeth), cy + inner_r * sin(a + pi / teeth)))

    draw.polygon(points, fill=color)
    draw.ellipse(
        (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
        fill=color,
    )
    draw.ellipse(
        (cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r),
        fill=(0, 0, 0, 0),
    )
    img = img.resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img)
