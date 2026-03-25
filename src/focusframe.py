#!/usr/bin/env python3
"""
FocusFrame — Active Window Border Highlighter for Windows
=========================================================
Draws a customizable colored border around the currently focused window.

Key design choices
------------------
  Overlay     : Pure Win32 layered window rendered with UpdateLayeredWindow,
                giving true per-pixel alpha (no chroma-key hack, no black glow).
  Glow        : Drawn INWARD from the window border, fading to transparent.
  Tracking    : Detector watches both HWND *and* window position so the overlay
                follows windows as they are moved or resized.
  Settings UI : tkinter Toplevel with sliders, opened from the tray menu.
  Threading   : Detector runs in a daemon thread; tray in another daemon thread;
                tkinter event loop owns the main thread.
"""

import json
import os
import sys
import ctypes
import ctypes.wintypes as wt
import winreg
import threading
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser

import win32gui
import win32con
import win32api
import pystray
from PIL import Image, ImageDraw

if sys.platform != "win32":
    sys.exit("FocusFrame only runs on Windows.")

# ─── Win32 structures needed for UpdateLayeredWindow ──────────────────────────

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          ctypes.c_uint32),
        ("biWidth",         ctypes.c_int32),
        ("biHeight",        ctypes.c_int32),
        ("biPlanes",        ctypes.c_uint16),
        ("biBitCount",      ctypes.c_uint16),
        ("biCompression",   ctypes.c_uint32),
        ("biSizeImage",     ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed",       ctypes.c_uint32),
        ("biClrImportant",  ctypes.c_uint32),
    ]

class _RGBQUAD(ctypes.Structure):
    _fields_ = [("b", ctypes.c_uint8), ("g", ctypes.c_uint8),
                ("r", ctypes.c_uint8), ("x", ctypes.c_uint8)]

class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp",             ctypes.c_uint8),   # AC_SRC_OVER = 0
        ("BlendFlags",          ctypes.c_uint8),
        ("SourceConstantAlpha", ctypes.c_uint8),   # 255 = use per-pixel alpha
        ("AlphaFormat",         ctypes.c_uint8),   # AC_SRC_ALPHA = 1
    ]

_ULW_ALPHA    = 2
_AC_SRC_OVER  = 0
_AC_SRC_ALPHA = 1

_GWL_EXSTYLE       = -20
_WS_EX_LAYERED     = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW  = 0x00000080
_WS_EX_NOACTIVATE  = 0x08000000


# ─── Config ───────────────────────────────────────────────────────────────────

class Config:
    DEFAULTS: dict = {
        "border_color":     "#0078D4",
        "border_thickness": 2,
        "glow_enabled":     True,
        "glow_radius":      8,
        "opacity":          0.9,
        "corner_radius":    0,
        "refresh_rate_ms":  50,
        "autostart":        False,
        "excluded_classes": ["Shell_TrayWnd", "Progman", "WorkerW"],
    }

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data: dict = dict(self.DEFAULTS)
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data.update(json.load(fh))
            except Exception as exc:
                print(f"[FocusFrame] Config load error: {exc}")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value) -> None:
        self.data[key] = value


# ─── Win32 overlay window ─────────────────────────────────────────────────────

def _wndproc(hwnd, msg, wParam, lParam):
    """Minimal WndProc — everything deferred to DefWindowProc."""
    return win32gui.DefWindowProc(hwnd, msg, wParam, lParam)


class OverlayWindow:
    """
    A borderless, always-on-top, click-through Win32 layered window.

    Rendering strategy
    ------------------
    UpdateLayeredWindow() is used instead of GDI paint or tkinter canvas.
    This gives true per-pixel alpha, so the glow can properly fade to
    transparent without any black-halo artifact.

    Glow direction
    --------------
    The border sits exactly on the target window's edges.
    Glow rings are drawn INWARD (into the window area), fading from the
    border color to fully transparent.  The window content below remains
    visible through the transparent glow pixels.
    """

    _CLASS = "FocusFrameOverlay"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._hwnd: int = self._create_window()
        self._visible: bool = False
        self._current_rect: tuple | None = None

    # ── Window creation ───────────────────────────────────────────────────────

    def _create_window(self) -> int:
        hinstance = win32api.GetModuleHandle(None)

        wc = win32gui.WNDCLASS()
        wc.hInstance     = hinstance
        wc.lpszClassName = self._CLASS
        wc.lpfnWndProc   = _wndproc
        wc.style         = 0
        wc.hCursor       = None
        wc.hbrBackground = None
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass  # Already registered (e.g. hot-reload)

        ex_style = (
            win32con.WS_EX_LAYERED    |
            win32con.WS_EX_TRANSPARENT |
            win32con.WS_EX_TOPMOST    |
            win32con.WS_EX_TOOLWINDOW |
            _WS_EX_NOACTIVATE
        )
        hwnd = win32gui.CreateWindowEx(
            ex_style, self._CLASS, "",
            win32con.WS_POPUP,
            0, 0, 100, 100,
            None, None, hinstance, None,
        )
        return hwnd

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def hwnd(self) -> int:
        return self._hwnd

    def update_border(self, rect: tuple) -> None:
        """
        Reposition the overlay and redraw the border around *rect*.
        *rect* = (left, top, right, bottom) in screen coordinates.
        """
        left, top, right, bottom = rect
        w = right - left
        h = bottom - top

        if w < 4 or h < 4:
            self.hide()
            return

        # Always redraw — position OR size may have changed
        if rect == self._current_rect:
            return
        self._current_rect = rect

        img = self._render(w, h)
        self._blit(img, left, top, w, h)

    def refresh(self) -> None:
        """Force a redraw with the current config (called after settings change)."""
        if self._current_rect:
            saved = self._current_rect
            self._current_rect = None
            self.update_border(saved)

    def hide(self) -> None:
        self._current_rect = None
        if self._visible:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            self._visible = False

    # ── Rendering (PIL → DIB → UpdateLayeredWindow) ───────────────────────────

    def _render(self, width: int, height: int) -> Image.Image:
        """
        Build the RGBA image:
          1. Glow rings — inward from the border edge, fading to alpha=0.
          2. Solid border — at the window's outer edge.
        """
        img  = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        col = self.config["border_color"]
        r   = int(col[1:3], 16)
        g   = int(col[3:5], 16)
        b   = int(col[5:7], 16)

        thickness   = max(1, int(self.config["border_thickness"]))
        glow_on     = bool(self.config["glow_enabled"])
        glow_radius = int(self.config["glow_radius"]) if glow_on else 0
        corner_r    = int(self.config["corner_radius"])
        opacity     = float(self.config["opacity"])
        border_a    = int(255 * opacity)

        half = min(width, height) // 2

        # ── Glow (inward, fading to transparent) ──────────────────────────────
        # Ring index i=0 → at the border (brightest); i=glow_radius-1 → deepest (alpha≈0)
        if glow_radius > 0:
            for i in range(glow_radius):
                fade  = i / glow_radius            # 0.0=bright → 1.0=transparent
                alpha = int(border_a * (1.0 - fade) * 0.75)
                if alpha < 2:
                    continue
                offset = thickness + i             # Move inward past the border
                if offset >= half:
                    break
                self._rect(draw,
                           offset, offset, width - offset - 1, height - offset - 1,
                           (r, g, b, alpha), 1,
                           max(0, corner_r - offset))

        # ── Solid border at window edges ───────────────────────────────────────
        for i in range(thickness):
            if i >= half:
                break
            self._rect(draw,
                       i, i, width - i - 1, height - i - 1,
                       (r, g, b, border_a), 1,
                       max(0, corner_r - i))

        return img

    @staticmethod
    def _rect(draw, x1, y1, x2, y2, color, lw, radius=0):
        """Draw a (optionally rounded) rectangle outline."""
        if radius > 0 and 2 * radius < min(x2 - x1, y2 - y1):
            r = radius
            draw.arc([x1,       y1,       x1+2*r, y1+2*r], 180, 270, fill=color, width=lw)
            draw.arc([x2-2*r,   y1,       x2,     y1+2*r], 270, 360, fill=color, width=lw)
            draw.arc([x2-2*r,   y2-2*r,   x2,     y2    ], 0,   90,  fill=color, width=lw)
            draw.arc([x1,       y2-2*r,   x1+2*r, y2    ], 90,  180, fill=color, width=lw)
            draw.line([x1+r, y1,  x2-r, y1 ], fill=color, width=lw)
            draw.line([x2,   y1+r, x2,  y2-r], fill=color, width=lw)
            draw.line([x1+r, y2,  x2-r, y2 ], fill=color, width=lw)
            draw.line([x1,   y1+r, x1,  y2-r], fill=color, width=lw)
        else:
            draw.rectangle([x1, y1, x2, y2], outline=color, width=lw)

    def _blit(self, img: Image.Image, dst_x: int, dst_y: int, w: int, h: int) -> None:
        """
        Push the RGBA image to the layered window via UpdateLayeredWindow.
        This replaces the entire window content (position + pixels) atomically.
        """
        # PIL stores RGBA; Windows DIB expects BGRA
        r_ch, g_ch, b_ch, a_ch = img.split()
        bgra_img = Image.merge("RGBA", (b_ch, g_ch, r_ch, a_ch))
        raw      = bgra_img.tobytes()

        hdc_screen = ctypes.windll.user32.GetDC(None)
        hdc_mem    = ctypes.windll.gdi32.CreateCompatibleDC(hdc_screen)

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize      = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth     = w
        bmi.bmiHeader.biHeight    = -h   # negative → top-down DIB
        bmi.bmiHeader.biPlanes    = 1
        bmi.bmiHeader.biBitCount  = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        pbits = ctypes.c_void_p()
        hbm   = ctypes.windll.gdi32.CreateDIBSection(
            hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(pbits), None, 0
        )
        ctypes.memmove(pbits, raw, len(raw))
        old_bm = ctypes.windll.gdi32.SelectObject(hdc_mem, hbm)

        pt_dst = _POINT(dst_x, dst_y)
        pt_src = _POINT(0, 0)
        sz     = _SIZE(w, h)
        bf     = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)

        # Show the window (no-op if already visible, never steals focus)
        if not self._visible:
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
            self._visible = True

        ctypes.windll.user32.UpdateLayeredWindow(
            self._hwnd,
            hdc_screen,
            ctypes.byref(pt_dst),
            ctypes.byref(sz),
            hdc_mem,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(bf),
            _ULW_ALPHA,
        )

        # Clean up GDI objects
        ctypes.windll.gdi32.SelectObject(hdc_mem, old_bm)
        ctypes.windll.gdi32.DeleteObject(hbm)
        ctypes.windll.gdi32.DeleteDC(hdc_mem)
        ctypes.windll.user32.ReleaseDC(None, hdc_screen)


# ─── Focus Detector ───────────────────────────────────────────────────────────

class FocusDetector:
    """
    Polls GetForegroundWindow() and GetWindowRect() in a background thread.

    The overlay is updated whenever:
      - The focused window changes (different HWND), OR
      - The focused window is moved / resized (same HWND, different rect).

    Both conditions are checked every refresh_rate_ms milliseconds, which is
    what makes the overlay follow windows smoothly as they are dragged.
    """

    _ALWAYS_EXCLUDE = {"Shell_TrayWnd", "Progman", "WorkerW"}

    def __init__(self, config: Config, overlay: OverlayWindow,
                 stop_event: threading.Event) -> None:
        self.config     = config
        self.overlay    = overlay
        self.stop_event = stop_event
        self._last_hwnd: int | None   = None
        self._last_rect: tuple | None = None

    def run(self) -> None:
        """Daemon thread entry point."""
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                print(f"[FocusFrame] Detector: {exc}")
            self.stop_event.wait(self.config["refresh_rate_ms"] / 1000.0)

    def _tick(self) -> None:
        hwnd = win32gui.GetForegroundWindow()

        if self._should_skip(hwnd):
            if self._last_hwnd is not None:
                self._last_hwnd = None
                self._last_rect = None
                self.overlay.hide()
            return

        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            self.overlay.hide()
            return

        # Skip if nothing changed — avoids unnecessary redraws
        if hwnd == self._last_hwnd and rect == self._last_rect:
            return

        self._last_hwnd = hwnd
        self._last_rect = rect
        self.overlay.update_border(rect)

    def _should_skip(self, hwnd: int) -> bool:
        if not hwnd:
            return True
        if win32gui.IsIconic(hwnd):        # Minimised window
            return True
        if hwnd == self.overlay.hwnd:      # Our own overlay
            return True
        try:
            cls = win32gui.GetClassName(hwnd)
        except Exception:
            return True
        user_excl = set(self.config["excluded_classes"])
        return cls in self._ALWAYS_EXCLUDE or cls in user_excl


# ─── Settings Window ──────────────────────────────────────────────────────────

class SettingsWindow:
    """
    A simple settings panel with sliders for all visual parameters.
    Changes are applied live to the overlay as sliders are moved.
    Opened from the tray right-click menu.
    """

    PRESETS = [
        ("Default Blue",  "#0078D4"), ("Red Alert",    "#FF3B30"),
        ("Green Focus",   "#30D158"), ("White Minimal", "#FFFFFF"),
        ("Amber Warm",    "#FF9F0A"), ("Violet Dream",  "#A78BFA"),
        ("Hot Pink",      "#FF375F"), ("Cyan Ice",      "#64D2FF"),
        ("Blue-Violet",   "#7C6CDB"), ("Yellow Neon",   "#FFD60A"),
        ("Sunset",        "#FF6B20"), ("Lime",          "#34C759"),
        ("Mint Ocean",    "#4AE4A0"), ("Purple Haze",   "#BF5AF2"),
        ("Crimson",       "#FF2D55"), ("Gold Rush",     "#FFBA0A"),
        ("Sky Blue",      "#5AC8FA"), ("Coral",         "#FF6482"),
        ("Gradient Fade", "#D080CD"), ("Emerald",       "#48DC7D"),
        ("iOS Blue",      "#007AFF"), ("Berry Blast",   "#DF44A4"),
        ("Steel Gray",    "#8E8E93"), ("Soft White",    "#E0E0E0"),
    ]

    def __init__(self, config: Config, overlay: OverlayWindow,
                 root: tk.Tk) -> None:
        self.config  = config
        self.overlay = overlay
        self.root    = root
        self._win: tk.Toplevel | None = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return
        self._build()

    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        self._win = win
        win.title("FocusFrame — Settings")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(padx=24, pady=16)

        frame = tk.Frame(win)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        row = 0

        # ── Border color ──────────────────────────────────────────────────────
        self._color = tk.StringVar(value=self.config["border_color"])
        tk.Label(frame, text="Border color", anchor="w").grid(
            row=row, column=0, sticky="w", padx=8, pady=6)
        self._color_btn = tk.Button(
            frame, bg=self._color.get(), width=8, relief="solid",
            command=self._pick_color,
        )
        self._color_btn.grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # ── Color presets ────────────────────────────────────────────────────
        tk.Label(frame, text="Presets", anchor="w").grid(
            row=row, column=0, sticky="nw", padx=8, pady=6)
        preset_frame = tk.Frame(frame)
        preset_frame.grid(row=row, column=1, sticky="w", padx=8, pady=6)
        cols = 6
        for idx, (name, hex_color) in enumerate(self.PRESETS):
            r_idx, c_idx = divmod(idx, cols)
            btn = tk.Button(
                preset_frame, bg=hex_color, width=2, height=1,
                relief="solid", borderwidth=1,
                command=lambda c=hex_color: self._set_preset(c),
            )
            btn.grid(row=r_idx, column=c_idx, padx=1, pady=1)
            btn.bind("<Enter>",
                     lambda e, n=name: self._win.title(f"FocusFrame — {n}"))
            btn.bind("<Leave>",
                     lambda e: self._win.title("FocusFrame — Settings"))
        row += 1

        # ── Sliders ───────────────────────────────────────────────────────────
        self._thickness = tk.IntVar(value=self.config["border_thickness"])
        row = self._slider(frame, row, "Border thickness",
                           self._thickness, 1, 10)

        self._glow_on = tk.BooleanVar(value=self.config["glow_enabled"])
        tk.Label(frame, text="Glow", anchor="w").grid(
            row=row, column=0, sticky="w", padx=8, pady=6)
        tk.Checkbutton(frame, variable=self._glow_on,
                       command=self._apply).grid(
            row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        self._glow_r = tk.IntVar(value=self.config["glow_radius"])
        row = self._slider(frame, row, "Glow radius", self._glow_r, 0, 40)

        self._opacity = tk.DoubleVar(value=self.config["opacity"])
        row = self._slider(frame, row, "Opacity",
                           self._opacity, 0.1, 1.0, res=0.05)

        self._corner = tk.IntVar(value=self.config["corner_radius"])
        row = self._slider(frame, row, "Corner radius", self._corner, 0, 30)

        self._refresh = tk.IntVar(value=self.config["refresh_rate_ms"])
        row = self._slider(frame, row, "Refresh rate (ms)",
                           self._refresh, 10, 200)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn = tk.Frame(frame)
        btn.grid(row=row, column=0, columnspan=2, pady=(14, 0))
        tk.Button(btn, text="Save & close", width=14,
                  command=self._save).pack(side="left", padx=4)
        tk.Button(btn, text="Close", width=10,
                  command=win.destroy).pack(side="left", padx=4)

        # Center on screen
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        ww, wh = win.winfo_reqwidth(),    win.winfo_reqheight()
        win.geometry(f"+{(sw-ww)//2}+{(sh-wh)//2}")

    def _slider(self, frame, row, label, var, lo, hi, res=1):
        tk.Label(frame, text=label, anchor="w").grid(
            row=row, column=0, sticky="w", padx=8, pady=4)
        tk.Scale(frame, variable=var, from_=lo, to=hi, orient="horizontal",
                 resolution=res, showvalue=True, length=200,
                 command=lambda _: self._apply()).grid(
            row=row, column=1, sticky="ew", padx=8, pady=4)
        return row + 1

    def _set_preset(self, hex_color: str) -> None:
        """Apply a preset color and update the UI."""
        self._color.set(hex_color)
        self._color_btn.configure(bg=hex_color)
        self._apply()

    def _pick_color(self) -> None:
        result = colorchooser.askcolor(
            color=self._color.get(),
            title="Choose border color",
            parent=self._win,
        )
        if result and result[1]:
            self._color.set(result[1])
            self._color_btn.configure(bg=result[1])
            self._apply()

    def _apply(self) -> None:
        """Push current UI values into config and refresh the overlay live."""
        self.config["border_color"]     = self._color.get()
        self.config["border_thickness"] = self._thickness.get()
        self.config["glow_enabled"]     = self._glow_on.get()
        self.config["glow_radius"]      = self._glow_r.get()
        self.config["opacity"]          = round(self._opacity.get(), 2)
        self.config["corner_radius"]    = self._corner.get()
        self.config["refresh_rate_ms"]  = self._refresh.get()
        self.overlay.refresh()

    def _save(self) -> None:
        self._apply()
        self.config.save()
        if self._win:
            self._win.destroy()


# ─── Tray Icon ────────────────────────────────────────────────────────────────

class TrayIcon:
    """System-tray icon with Settings, Start with Windows, and Quit."""

    APP = "FocusFrame"

    def __init__(self, config: Config, overlay: OverlayWindow,
                 settings: SettingsWindow, stop_event: threading.Event,
                 root: tk.Tk) -> None:
        self.config     = config
        self.overlay    = overlay
        self.settings   = settings
        self.stop_event = stop_event
        self.root       = root

    def _icon_image(self, size: int = 64) -> Image.Image:
        img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        c    = self.config["border_color"]
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        m, lw = size // 6, max(3, size // 10)
        draw.rectangle([m, m, size - m, size - m], outline=(r, g, b, 255), width=lw)
        return img

    # ── Autostart ─────────────────────────────────────────────────────────────

    def _launch_cmd(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'

    def _set_autostart(self, enable: bool) -> None:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                 winreg.KEY_SET_VALUE)
            if enable:
                winreg.SetValueEx(key, self.APP, 0, winreg.REG_SZ,
                                  self._launch_cmd())
            else:
                try:
                    winreg.DeleteValue(key, self.APP)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as exc:
            print(f"[FocusFrame] Autostart: {exc}")

    # ── Menu callbacks ─────────────────────────────────────────────────────────

    def _on_settings(self, icon, item) -> None:
        # Must run on the main (tkinter) thread
        self.root.after(0, self.settings.show)

    def _on_autostart(self, icon, item) -> None:
        new = not self.config["autostart"]
        self.config["autostart"] = new
        self.config.save()
        self._set_autostart(new)

    def _on_quit(self, icon, item) -> None:
        self.stop_event.set()
        icon.stop()
        self.root.after(0, self.root.quit)

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem(self.APP, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings",          self._on_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows", self._on_autostart,
                checked=lambda _: bool(self.config["autostart"]),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        pystray.Icon(self.APP, self._icon_image(), self.APP, menu).run()


# ─── Entry point ──────────────────────────────────────────────────────────────

def _locate_config() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "config" / "config.json"
    return Path(__file__).resolve().parent.parent / "config" / "config.json"


def main() -> None:
    # Enable per-monitor DPI awareness before any window is created
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    config     = Config(str(_locate_config()))
    stop_event = threading.Event()

    # Overlay is a pure Win32 window (no tkinter involved in its rendering)
    overlay = OverlayWindow(config)

    # Hidden tkinter root — only used for the Settings Toplevel + message pump
    root = tk.Tk()
    root.withdraw()

    settings = SettingsWindow(config, overlay, root)
    tray     = TrayIcon(config, overlay, settings, stop_event, root)
    detector = FocusDetector(config, overlay, stop_event)

    # Pump Win32 messages for the overlay window from the tkinter event loop
    def _pump():
        win32gui.PumpWaitingMessages()
        if not stop_event.is_set():
            root.after(32, _pump)

    root.after(32, _pump)

    threading.Thread(target=detector.run, daemon=True, name="Detector").start()
    threading.Thread(target=tray.run,     daemon=True, name="TrayIcon").start()

    root.mainloop()


if __name__ == "__main__":
    main()
