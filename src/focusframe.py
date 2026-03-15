#!/usr/bin/env python3
"""
FocusFrame — Active Window Border Highlighter for Windows
=========================================================
Draws a customizable colored border around whichever window currently has
keyboard focus. Runs as a system-tray application with no visible window of
its own.

Architecture overview
---------------------
  Main thread  : tkinter event loop — owns and updates the overlay window.
  Detector     : background daemon — polls GetForegroundWindow() and schedules
                 overlay updates via root.after() (thread-safe).
  TrayIcon     : background daemon — runs the pystray message loop and handles
                 the "Start with Windows" and "Quit" menu items.
"""

import json
import os
import sys
import ctypes
import winreg
import threading
from pathlib import Path

import win32gui
import pystray
from PIL import Image, ImageDraw

import tkinter as tk

# ─── Platform guard ───────────────────────────────────────────────────────────
if sys.platform != "win32":
    sys.exit("FocusFrame only runs on Windows.")

# ─── Win32 extended-style constants ───────────────────────────────────────────
GWL_EXSTYLE       = -20
WS_EX_LAYERED     = 0x00080000   # Required for transparency / color-key
WS_EX_TRANSPARENT = 0x00000020   # Mouse events pass through to windows below
WS_EX_TOOLWINDOW  = 0x00000080   # Hidden from taskbar and Alt+Tab
WS_EX_NOACTIVATE  = 0x08000000   # Clicking the overlay does not steal focus


# ─── Config ───────────────────────────────────────────────────────────────────
class Config:
    """
    Thin wrapper around config.json.  Missing keys fall back to DEFAULTS so
    users can add only the settings they want to override.
    """

    DEFAULTS: dict = {
        "border_color":     "#0078D4",   # Default: Windows accent blue
        "border_thickness": 3,           # Pixels
        "glow_enabled":     True,
        "glow_radius":      12,          # Pixels of glow expansion
        "opacity":          0.92,        # 0.0 – 1.0 (overall overlay opacity)
        "corner_radius":    0,           # 0 = sharp corners
        "refresh_rate_ms":  50,          # How often to poll for window changes
        "autostart":        False,       # Whether to launch at Windows login
        # Window classes that should never receive an overlay:
        "excluded_classes": [
            "Shell_TrayWnd",             # Windows taskbar
            "Progman",                   # Desktop
            "WorkerW",                   # Desktop wallpaper worker
        ],
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
            except (json.JSONDecodeError, IOError) as exc:
                print(f"[FocusFrame] Config load error ({exc}) — using defaults.")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value) -> None:
        self.data[key] = value


# ─── Overlay Window ───────────────────────────────────────────────────────────
class OverlayWindow:
    """
    A borderless, always-on-top, click-through tkinter window used to draw
    the highlight border.

    Transparency strategy
    ---------------------
    We configure tkinter with ``-transparentcolor black``: every pixel painted
    pure black (#000000) becomes fully transparent at the OS level.  We use
    black as the canvas background, so only the border / glow shapes are
    visible.  Glow colors are interpolated toward black (never *exactly* black)
    to simulate a fade-out halo.
    """

    _CHROMA_KEY = "black"   # The color that becomes transparent

    def __init__(self, config: Config) -> None:
        self.config = config
        self._current_rect: tuple | None = None
        self._hwnd: int | None = None

        self._enable_dpi_awareness()
        self._create_window()

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _enable_dpi_awareness(self) -> None:
        """
        Per-monitor DPI awareness ensures that GetWindowRect coordinates are
        accurate on mixed-DPI multi-monitor setups.
        """
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    def _create_window(self) -> None:
        self.root = tk.Tk()
        self.root.title("FocusFrameOverlay")       # Used to find HWND later
        self.root.overrideredirect(True)            # Remove all window decorations
        self.root.wm_attributes("-topmost",         True)
        self.root.wm_attributes("-transparentcolor", self._CHROMA_KEY)
        self.root.wm_attributes("-alpha",           self.config["opacity"])
        self.root.configure(bg=self._CHROMA_KEY)
        self.root.withdraw()                        # Hidden until the first focus event

        self.canvas = tk.Canvas(
            self.root,
            bg=self._CHROMA_KEY,
            highlightthickness=0,
            cursor="none",
        )
        self.canvas.pack(fill="both", expand=True)

        # The window must be realized (mapped) before we can read its HWND.
        self.root.update()
        self._patch_win32_styles()

    def _patch_win32_styles(self) -> None:
        """
        Add Win32 extended styles that tkinter does not expose:
          WS_EX_TRANSPARENT  — passes all mouse/touch input to the window below.
          WS_EX_TOOLWINDOW   — hides from taskbar and Alt+Tab.
          WS_EX_NOACTIVATE   — prevents stealing keyboard focus.
        WS_EX_LAYERED is already set by tkinter when -transparentcolor is used.
        """
        hwnd  = self.get_hwnd()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )

    # ── Public interface ───────────────────────────────────────────────────────

    def get_hwnd(self) -> int:
        """Return the Win32 HWND for the overlay, caching after first lookup."""
        if self._hwnd is None:
            self._hwnd = ctypes.windll.user32.FindWindowW(None, "FocusFrameOverlay")
        return self._hwnd

    def update_border(self, rect: tuple) -> None:
        """
        Reposition the overlay and redraw the border around *rect*.
        *rect* is (left, top, right, bottom) in screen coordinates, exactly
        as returned by win32gui.GetWindowRect().
        """
        if rect == self._current_rect:
            return
        self._current_rect = rect

        left, top, right, bottom = rect
        win_w_target = right  - left
        win_h_target = bottom - top

        thickness   = self.config["border_thickness"]
        glow_on     = self.config["glow_enabled"]
        glow_radius = self.config["glow_radius"] if glow_on else 0

        # Padding = room for glow + border outside the target window edge
        padding = glow_radius + thickness + 2

        # Size and position the overlay so it encompasses the entire glow area
        ov_x = left  - padding
        ov_y = top   - padding
        ov_w = win_w_target + 2 * padding
        ov_h = win_h_target + 2 * padding

        self.root.geometry(f"{ov_w}x{ov_h}+{ov_x}+{ov_y}")
        self.root.deiconify()
        self.root.lift()

        self._draw(ov_w, ov_h, padding, thickness, glow_radius)

    def hide(self) -> None:
        self._current_rect = None
        self.root.withdraw()

    def mainloop(self) -> None:
        self.root.mainloop()

    # ── Drawing ────────────────────────────────────────────────────────────────

    def _draw(
        self,
        ov_w: int,
        ov_h: int,
        padding: int,
        thickness: int,
        glow_radius: int,
    ) -> None:
        """
        Clear the canvas and redraw glow + border.

        Coordinate system (relative to the overlay window):
          (padding, padding)  →  exact left-top corner of the target window
          (ov_w-padding, ov_h-padding)  →  right-bottom corner
        """
        self.canvas.delete("all")

        color  = self.config["border_color"]
        radius = self.config["corner_radius"]

        # Inner rectangle aligns with the target window's edges
        x1, y1 = padding, padding
        x2, y2 = ov_w - padding, ov_h - padding

        # ── Glow layers ──────────────────────────────────────────────────────
        # Draw concentric rings expanding outward from the border.
        # Each ring is colored by fading the border color toward black;
        # because black is the chroma-key, the rings gradually "disappear".
        if glow_radius > 0:
            for i in range(glow_radius, 0, -1):
                fade_factor = i / glow_radius          # 1 = far out (dark), 0 = near border (bright)
                glow_color  = self._fade_to_black(color, fade_factor)
                self._draw_rect(
                    x1 - i, y1 - i, x2 + i, y2 + i,
                    glow_color, 1, max(0, radius + i),
                )

        # ── Border rings ─────────────────────────────────────────────────────
        # Draw the solid border as multiple 1-pixel-wide concentric rings
        # expanding outward from the window edge.
        for i in range(thickness):
            self._draw_rect(
                x1 - i, y1 - i, x2 + i, y2 + i,
                color, 1, max(0, radius + i),
            )

    def _draw_rect(
        self,
        x1: float, y1: float, x2: float, y2: float,
        color: str, width: int, radius: int,
    ) -> None:
        """Draw a (rounded) rectangle outline on the canvas."""
        if radius > 0 and 2 * radius < min(x2 - x1, y2 - y1):
            r = radius
            # Four corner arcs
            corners = [
                (x1,       y1,       90),   # Top-left
                (x2 - 2*r, y1,        0),   # Top-right
                (x2 - 2*r, y2 - 2*r, 270), # Bottom-right
                (x1,       y2 - 2*r, 180), # Bottom-left
            ]
            for cx, cy, start in corners:
                self.canvas.create_arc(
                    cx, cy, cx + 2*r, cy + 2*r,
                    start=start, extent=90,
                    outline=color, style="arc", width=width,
                )
            # Four straight edges connecting the arcs
            self.canvas.create_line(x1 + r, y1, x2 - r, y1, fill=color, width=width)
            self.canvas.create_line(x2, y1 + r, x2, y2 - r, fill=color, width=width)
            self.canvas.create_line(x1 + r, y2, x2 - r, y2, fill=color, width=width)
            self.canvas.create_line(x1, y1 + r, x1, y2 - r, fill=color, width=width)
        else:
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=width)

    def _fade_to_black(self, color: str, factor: float) -> str:
        """
        Interpolate *color* toward black by *factor* (0.0 = original color,
        1.0 = near-black).  The result is never exactly #000000 so the chroma-
        key never accidentally swallows visible glow pixels.
        """
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        r = max(1, int(r * (1.0 - factor)))
        g = max(1, int(g * (1.0 - factor)))
        b = max(1, int(b * (1.0 - factor)))
        return f"#{r:02x}{g:02x}{b:02x}"


# ─── Focus Detector ───────────────────────────────────────────────────────────
class FocusDetector:
    """
    Polls win32gui.GetForegroundWindow() in a background thread at the
    configured refresh rate.  When the active window changes, it schedules an
    overlay update on the tkinter main thread using root.after(0, ...).

    Using root.after() is the only thread-safe way to call tkinter from outside
    the main thread.
    """

    # Window classes that are always excluded, regardless of config
    _ALWAYS_EXCLUDE = {"Shell_TrayWnd", "Progman", "WorkerW"}

    def __init__(
        self,
        config: Config,
        overlay: OverlayWindow,
        stop_event: threading.Event,
    ) -> None:
        self.config      = config
        self.overlay     = overlay
        self.stop_event  = stop_event
        self._last_hwnd: int | None = None
        self._overlay_hwnd: int | None = None   # Filled in after overlay is visible

    def run(self) -> None:
        """Entry point for the detector daemon thread."""
        interval = self.config["refresh_rate_ms"] / 1000.0
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                print(f"[FocusFrame] Detector error: {exc}")
            self.stop_event.wait(interval)

    def _tick(self) -> None:
        """One polling iteration: check if foreground window has changed."""
        hwnd = win32gui.GetForegroundWindow()

        # Skip if same window as last frame — no work to do
        if hwnd == self._last_hwnd:
            return
        self._last_hwnd = hwnd

        if self._should_skip(hwnd):
            # Schedule hide() on the tkinter thread
            self.overlay.root.after(0, self.overlay.hide)
            return

        try:
            # GetWindowRect returns (left, top, right, bottom) in screen coords
            rect = win32gui.GetWindowRect(hwnd)
            self.overlay.root.after(0, lambda r=rect: self.overlay.update_border(r))
        except Exception:
            self.overlay.root.after(0, self.overlay.hide)

    def _should_skip(self, hwnd: int) -> bool:
        """Return True if this window should not receive a border overlay."""
        if not hwnd:
            return True

        # Minimized (iconic) windows have no meaningful visible area
        if win32gui.IsIconic(hwnd):
            return True

        # Never highlight our own overlay window
        if self._overlay_hwnd and hwnd == self._overlay_hwnd:
            return True

        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            return True

        user_excluded = set(self.config["excluded_classes"])
        return class_name in self._ALWAYS_EXCLUDE or class_name in user_excluded


# ─── Tray Icon ────────────────────────────────────────────────────────────────
class TrayIcon:
    """
    System-tray icon with a minimal context menu:
      • Start with Windows  (toggle, persisted to config + registry)
      • Quit
    """

    APP_NAME = "FocusFrame"

    def __init__(
        self,
        config: Config,
        overlay: OverlayWindow,
        stop_event: threading.Event,
    ) -> None:
        self.config     = config
        self.overlay    = overlay
        self.stop_event = stop_event
        self._icon: pystray.Icon | None = None

    # ── Icon image ─────────────────────────────────────────────────────────────

    def _make_image(self, size: int = 64) -> Image.Image:
        """Generate a small frame icon using the configured border color."""
        img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        color = self.config["border_color"]
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)

        margin     = size // 6
        line_width = max(3, size // 10)
        draw.rectangle(
            [margin, margin, size - margin, size - margin],
            outline=(r, g, b, 255),
            width=line_width,
        )
        return img

    # ── Autostart (Windows registry) ───────────────────────────────────────────

    def _build_launch_command(self) -> str:
        """
        Return the command string that should be stored in the registry.
        Handles both the compiled-exe case (PyInstaller) and plain-script case.
        """
        if getattr(sys, "frozen", False):
            # Running as a standalone .exe built by PyInstaller
            return f'"{sys.executable}"'
        # Running as a plain Python script
        script = os.path.abspath(sys.argv[0])
        return f'"{sys.executable}" "{script}"'

    def _apply_autostart(self, enable: bool) -> None:
        """Write or remove the registry Run entry for FocusFrame."""
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
            )
            if enable:
                winreg.SetValueEx(
                    key, self.APP_NAME, 0, winreg.REG_SZ,
                    self._build_launch_command(),
                )
            else:
                try:
                    winreg.DeleteValue(key, self.APP_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as exc:
            print(f"[FocusFrame] Autostart registry error: {exc}")

    # ── Menu callbacks ─────────────────────────────────────────────────────────

    def _on_toggle_autostart(self, icon: pystray.Icon, item) -> None:
        new_value = not self.config["autostart"]
        self.config["autostart"] = new_value
        self.config.save()
        self._apply_autostart(new_value)

    def _on_quit(self, icon: pystray.Icon, item) -> None:
        self.stop_event.set()
        icon.stop()
        # Ask tkinter to exit its event loop from the main thread
        self.overlay.root.after(0, self.overlay.root.quit)

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the pystray icon.  Blocks until icon.stop() is called."""
        menu = pystray.Menu(
            pystray.MenuItem(self.APP_NAME, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows",
                self._on_toggle_autostart,
                checked=lambda _: bool(self.config["autostart"]),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        self._icon = pystray.Icon(
            self.APP_NAME,
            self._make_image(),
            self.APP_NAME,
            menu,
        )
        self._icon.run()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _locate_config() -> Path:
    """
    Return the path to config/config.json relative to the project root.

    When frozen by PyInstaller (sys.frozen == True), sys.executable is the
    .exe file; the config folder is expected next to it.
    When running as a plain script, __file__ is src/focusframe.py, so the
    project root is one level up.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "config" / "config.json"
    return Path(__file__).resolve().parent.parent / "config" / "config.json"


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    config     = Config(str(_locate_config()))
    stop_event = threading.Event()
    overlay    = OverlayWindow(config)
    detector   = FocusDetector(config, overlay, stop_event)
    tray       = TrayIcon(config, overlay, stop_event)

    # Let the detector know which HWND is ours so it never highlights itself
    overlay.root.update()
    detector._overlay_hwnd = overlay.get_hwnd()

    # Detector runs in a daemon thread — dies automatically when main exits
    threading.Thread(
        target=detector.run, daemon=True, name="FocusDetector"
    ).start()

    # Tray icon also runs in a daemon thread
    threading.Thread(
        target=tray.run, daemon=True, name="TrayIcon"
    ).start()

    # tkinter's event loop must run on the main thread
    overlay.mainloop()


if __name__ == "__main__":
    main()
