# FocusFrame

**Active Window Border Highlighter for Windows 11**

FocusFrame runs silently in your system tray and draws a customizable colored
border around whichever window currently has keyboard focus.  It never modifies
the target window and never intercepts your mouse or keyboard input.

---

## The problem it solves

When you work with many windows open at once — especially multiple instances of
the same application (several VS Code editors, browser windows, terminals) or
across two or more monitors — it can be surprisingly hard to tell at a glance
which window is active.  A misplaced keystroke goes to the wrong window.  You
type a command into the wrong terminal.  You close the wrong document.

FocusFrame eliminates that ambiguity with an immediate, unambiguous visual cue.

### Typical use cases

- **Multi-monitor setups** — one focused window among several; instantly obvious.
- **Multiple VS Code instances** — same UI, different projects; no more confusion.
- **Tiling window managers** — know exactly where your keystrokes will land.
- **Remote desktop / KVM** — understand which session is receiving input.
- **Screen sharing / streaming** — viewers can follow which window you are using.

---

## Features

- Colored overlay border drawn by a transparent, always-on-top window overlay
- Border never interferes with clicks, scrolling, or keyboard input (fully click-through)
- Glow / halo effect with configurable radius
- Configurable color, thickness, opacity, corner radius, and refresh rate
- System-tray icon — no visible window, no taskbar entry, no Alt+Tab clutter
- Optional autostart at Windows login (registry-based toggle in the tray menu)
- Lightweight: idle CPU usage is negligible; the overlay only redraws when the focused window changes
- Ready to be packaged as a standalone `.exe` with PyInstaller

---

## Requirements

- Windows 10 / 11 (64-bit)
- Python 3.11 or later
- Dependencies listed in `requirements.txt`

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-username/FocusFrame.git
cd FocusFrame

# 2. (Recommended) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Running FocusFrame

```bash
python src/focusframe.py
```

FocusFrame starts immediately, places an icon in your system tray, and begins
highlighting the active window.  There is no visible launcher window.

To exit, right-click the tray icon and choose **Quit**.

---

## Configuration

All settings live in `config/config.json`.  Edit the file, save it, and restart
FocusFrame to apply changes.

| Key | Type | Default | Description |
|---|---|---|---|
| `border_color` | string | `"#0078D4"` | Hex color of the border and glow |
| `border_thickness` | integer | `3` | Border width in pixels |
| `glow_enabled` | boolean | `true` | Whether to draw the glow halo |
| `glow_radius` | integer | `12` | Halo size in pixels (only used when `glow_enabled` is `true`) |
| `opacity` | float | `0.92` | Overall overlay opacity, `0.0` (invisible) to `1.0` (fully opaque) |
| `corner_radius` | integer | `0` | Corner rounding radius in pixels; `0` = sharp corners |
| `refresh_rate_ms` | integer | `50` | How often (ms) FocusFrame polls for window changes; lower = faster but more CPU |
| `autostart` | boolean | `false` | Mirrors the "Start with Windows" toggle in the tray menu — prefer using the menu |
| `excluded_classes` | array | see below | Win32 window class names that should never receive an overlay |

### Default excluded classes

```json
["Shell_TrayWnd", "Progman", "WorkerW"]
```

These correspond to the Windows taskbar and desktop — you almost certainly do
not want a border around those.  You can add any Win32 class name to this list
(use [Spy++](https://learn.microsoft.com/en-us/visualstudio/debugger/spy-increment) or
`win32gui.GetClassName()` to find a window's class name).

### Example: red border, no glow, thicker line

```json
{
  "border_color": "#FF3B30",
  "border_thickness": 5,
  "glow_enabled": false,
  "opacity": 1.0,
  "refresh_rate_ms": 50
}
```

### Example: subtle white border with glow on dark desktop

```json
{
  "border_color": "#FFFFFF",
  "border_thickness": 2,
  "glow_enabled": true,
  "glow_radius": 20,
  "opacity": 0.75
}
```

---

## Building a standalone Windows executable

Use [PyInstaller](https://pyinstaller.org) to create a single `.exe` that runs
without a Python installation.

```bash
pip install pyinstaller

pyinstaller \
  --onefile \
  --noconsole \
  --name FocusFrame \
  --hidden-import win32gui \
  --hidden-import win32con \
  --hidden-import win32api \
  --hidden-import pystray._win32 \
  src/focusframe.py
```

The resulting `dist/FocusFrame.exe` can be distributed and run on any
64-bit Windows 10/11 machine.  Place a `config/` folder next to the `.exe`
so FocusFrame can find `config.json`.

> **Note:** `--noconsole` suppresses the terminal window.  During development
> you may want to omit it to see log output.

---

## Project structure

```
FocusFrame/
├── src/
│   └── focusframe.py     # All application code (overlay, detector, tray)
├── config/
│   └── config.json       # User configuration
├── assets/               # Placeholder for future icons / resources
├── README.md
├── LICENSE               # MIT
├── requirements.txt
└── .gitignore
```

---

## How it works

1. **Focus detection** — A background thread polls `GetForegroundWindow()` every
   `refresh_rate_ms` milliseconds.  When the returned handle changes, it reads
   the new window's position with `GetWindowRect()`.

2. **Overlay window** — A borderless tkinter window is kept always-on-top.  Its
   background color is set as a chroma-key (`-transparentcolor black`), making
   every black pixel fully transparent at the OS level.  The border and glow are
   drawn on a canvas; everything else remains transparent.

3. **Click-through** — Win32 extended styles `WS_EX_TRANSPARENT` and
   `WS_EX_NOACTIVATE` ensure that the overlay window neither captures mouse
   events nor steals keyboard focus.

4. **Glow effect** — Concentric rectangles are drawn expanding outward from the
   border, each colored by interpolating the border color toward black.  As the
   color approaches black it blends into the transparent background, creating a
   soft fade-out halo.

5. **Thread safety** — All tkinter calls from the detector thread are marshalled
   to the main thread via `root.after(0, callback)`, the only thread-safe way
   to interact with tkinter.

---

## Contributing

Bug reports, feature requests, and pull requests are welcome.  Please open an
issue first to discuss significant changes.

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes with a clear message
4. Open a pull request against `main`

---

## License

[MIT](LICENSE) — free for personal and commercial use.
