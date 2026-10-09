import ctypes
import ctypes.wintypes as wintypes
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import ttk

import psutil
import win32api
import win32con
import win32gui
import win32process


# ============================================================
# CONFIGURATION
# ============================================================

LIST_REFRESH_INTERVAL = 10
DEFAULT_CURSOR_BORDER = 20
CHECK_INTERVAL = 0.001
HWND_CACHE_TIME = 2.0

WEBSITE_URL = "https://www.itsmeprince.com"


# ============================================================
# THEME  —  stone / black / dark gray
# ============================================================

BG_WINDOW    = "#0c0a09"
BG_MAIN      = "#0c0a09"
BG_CARD      = "#1c1917"
BG_INPUT     = "#171412"
BG_HOVER     = "#292524"

FG_PRIMARY   = "#fafaf9"
FG_SECONDARY = "#a8a29e"
FG_MUTED     = "#78716c"

ACCENT       = "#e7e5e4"
ACCENT_HOVER = "#d6d3d1"

BTN_DARK        = "#292524"
BTN_DARK_HOVER  = "#44403c"
BTN_DISABLED_BG = "#1c1917"
BTN_DISABLED_FG = "#57534e"

STOP_BG         = "#2a1a1a"
STOP_BG_HOVER   = "#3a2020"
STOP_FG         = "#f87171"

SUCCESS = "#4ade80"
WARNING = "#fbbf24"
DANGER  = "#f87171"

BORDER_COLOR = "#292524"
SELECT_BG    = "#44403c"

SCROLL_TRACK = "#171412"
SCROLL_THUMB = "#44403c"
SCROLL_HOVER = "#57534e"

SLIDER_TRACK      = "#292524"
SLIDER_FILL       = "#a8a29e"
SLIDER_THUMB      = "#fafaf9"
SLIDER_THUMB_RING = "#0c0a09"


# ============================================================
# WINDOWS API
# ============================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.ClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
user32.ClipCursor.restype = wintypes.BOOL

user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL

user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL

gdi32.CreateRoundRectRgn.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int,
]
gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p

user32.SetWindowRgn.argtypes = [
    wintypes.HWND, ctypes.c_void_p, wintypes.BOOL,
]
user32.SetWindowRgn.restype = ctypes.c_int

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long

user32.SetWindowLongW.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_long,
]
user32.SetWindowLongW.restype = ctypes.c_long

GWL_EXSTYLE       = -20
WS_EX_TOOLWINDOW  = 0x00000080
WS_EX_APPWINDOW   = 0x00040000


# ============================================================
# BUG FIX NOTES  —  multi-monitor / Genshin Impact
# ============================================================
#
# Symptom reported by the user:
#   Game runs on MONITOR 1 (primary).  Cursor-lock starts fine.
#   But as soon as an in-game menu is opened, the cursor is
#   clipped to MONITOR 2 (left monitor) instead.
#
# Root cause:
#   1. find_target_window() used to return the FIRST visible
#      window belonging to the target PID.  EnumWindows walks
#      in Z-order (top-most first), so the moment the game
#      spawned a transient helper / overlay / cursor window,
#      THAT window was returned instead of the main game
#      window.  Genshin creates such small helper windows on
#      the PRIMARY monitor — which is what caused the jump.
#
#   2. get_monitor_rect_from_window() then took the CENTRE
#      POINT of that small helper window and asked Windows
#      which monitor it was on.  Result: primary monitor.
#
#   3. get_target_hwnd() re-scanned every 2 s, so a transient
#      helper window could silently replace the real game
#      window mid-session.
#
# Fix implemented below:
#   A. find_target_window() now enumerates EVERY visible window
#      of the target PID and keeps the one with the LARGEST
#      client area.  The main game window is always the
#      biggest, so overlays / cursor helpers are ignored.
#
#   B. get_monitor_rect_from_window() no longer relies on a
#      single point.  It intersects the game window rect with
#      every monitor and picks the monitor that covers the
#      most area.  This also handles games whose reported
#      window rect spans multiple monitors.
#
#   C. get_target_hwnd() prefers to keep an already-valid HWND
#      instead of blindly re-scanning every 2 s.  A single
#      stable HWND means the derived monitor rect is stable
#      for the whole locking session.
# ============================================================


# ============================================================
# GLOBAL STATE
# ============================================================

running = True
enabled = False

cursor_locked = False
current_rect = None

target_pids = set()
target_name = ""

cursor_border = DEFAULT_CURSOR_BORDER

last_foreground_state = None

worker_thread = None
scanner_thread = None

list_visible = True
current_entries = []
refresh_event = threading.Event()

cached_hwnd = None
cached_hwnd_time = 0.0
cached_rect = None
cached_rect_hwnd = None
cached_rect_border = None


# ============================================================
# ROUNDED RECTANGLE HELPER
# ============================================================

def draw_rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    r = int(min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    if r < 1:
        r = 1

    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]

    return canvas.create_polygon(points, smooth=True, **kwargs)


# ============================================================
# WINDOW HELPERS
# ============================================================

def get_window_process_id(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception:
        return None


def _window_area(hwnd):
    """Return the on-screen area (w*h) of a window, or 0 on failure."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w = right - left
        h = bottom - top
        if w <= 0 or h <= 0:
            return 0
        return w * h
    except Exception:
        return 0


def find_target_window():
    """
    Return the HWND of the largest visible window belonging to
    any of the target PIDs.

    FIX (see "BUG FIX NOTES" above): we no longer take the first
    match from EnumWindows.  Instead we keep the biggest window,
    which in practice is always the real game window.  Overlay
    windows, cursor helpers, tooltips, launchers, etc. are
    smaller and therefore ignored.
    """
    if not target_pids:
        return None

    # pid -> (area, hwnd)
    best_by_pid = {}

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True

            pid = get_window_process_id(hwnd)
            if pid is None or pid not in target_pids:
                return True

            # Reject iconic (minimised) windows — GetWindowRect
            # returns a bogus off-screen rect for them.
            try:
                if win32gui.IsIconic(hwnd):
                    return True
            except Exception:
                pass

            area = _window_area(hwnd)
            if area <= 0:
                return True

            # Reject suspiciously tiny windows outright.  These
            # are almost always helper windows the game creates
            # for the system cursor or for input capture.
            if area < 200 * 200:
                return True

            prev = best_by_pid.get(pid)
            if prev is None or area > prev[0]:
                best_by_pid[pid] = (area, hwnd)

        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        pass

    # Prefer whichever target PID we actually found.
    for pid in target_pids:
        entry = best_by_pid.get(pid)
        if entry is not None:
            return entry[1]

    return None


def get_target_hwnd():
    """
    Return a stable HWND for the currently-targeted process.

    FIX (see "BUG FIX NOTES" above): prefer an already-valid
    cached HWND.  The original code re-scanned every 2 s, which
    could silently swap the main game window for a transient
    helper window — and thereby swap the monitor.
    """
    global cached_hwnd, cached_hwnd_time

    # Reuse the cached HWND as long as it is still alive,
    # visible, and still belongs to one of our target PIDs.
    if cached_hwnd:
        try:
            if (win32gui.IsWindow(cached_hwnd)
                    and win32gui.IsWindowVisible(cached_hwnd)
                    and not win32gui.IsIconic(cached_hwnd)):
                cached_pid = get_window_process_id(cached_hwnd)
                if cached_pid in target_pids:
                    return cached_hwnd
        except Exception:
            pass

    # Cache is stale or invalid → full re-scan.
    now = time.time()
    if cached_hwnd and (now - cached_hwnd_time) < HWND_CACHE_TIME:
        return cached_hwnd

    cached_hwnd = find_target_window()
    cached_hwnd_time = now

    return cached_hwnd


def invalidate_window_cache():
    global cached_hwnd, cached_hwnd_time
    global cached_rect, cached_rect_hwnd, cached_rect_border

    cached_hwnd = None
    cached_hwnd_time = 0.0
    cached_rect = None
    cached_rect_hwnd = None
    cached_rect_border = None


# ============================================================
# FOREGROUND CHECK
# ============================================================

def is_target_foreground():
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return False

    pid = get_window_process_id(hwnd)
    if not pid:
        return False

    return pid in target_pids


# ============================================================
# MONITOR DETECTION
# ============================================================

def get_monitors():
    """
    Return a list of monitor info dicts (keys: Monitor, Work, Flags).

    Notes:
      * pywin32's `win32api.EnumDisplayMonitors` takes only
        (hdc, cliprect) and returns a list of
        (hmonitor, hdc, rect) tuples — it does NOT accept a
        callback the way the raw Win32 API does.
      * `hmonitor` comes back as a `PyHANDLE`.  Pylance's stub for
        `GetMonitorInfo` expects an `int`, so we coerce it.  At
        runtime `PyHANDLE` and `int` are the same value.
    """
    monitors = []

    try:
        for hmonitor, _hdc, _rect in win32api.EnumDisplayMonitors(None, None):
            try:
                monitors.append(win32api.GetMonitorInfo(int(hmonitor)))
            except Exception:
                pass
    except Exception:
        pass

    return monitors


def _intersection_area(rect_a, rect_b):
    """Area of the intersection between two (l,t,r,b) rectangles."""
    al, at, ar, ab = rect_a
    bl, bt, br, bb = rect_b
    il = max(al, bl)
    it = max(at, bt)
    ir = min(ar, br)
    ib = min(ab, bb)
    if ir <= il or ib <= it:
        return 0
    return (ir - il) * (ib - it)


def get_monitor_rect_from_window(hwnd):
    """
    Return the (left, top, right, bottom) rect of the monitor that
    the given window lives on.

    FIX (see "BUG FIX NOTES" above): the original implementation
    used a single centre point and asked the OS which monitor
    that point was on.  That fails whenever the window is not
    actually centred on the monitor it belongs to — exactly what
    happened when Genshin's small helper window on the primary
    monitor was mistaken for the game window.

    The new implementation intersects the window rect with every
    monitor and picks the monitor with the LARGEST overlap.
    This is the same rule Windows itself uses for "which monitor
    is this window on?" and it also tolerates games whose window
    rect spans multiple monitors.
    """
    try:
        win_rect = win32gui.GetWindowRect(hwnd)
        wl, wt, wr, wb = win_rect
        if wr <= wl or wb <= wt:
            return None

        monitors = get_monitors()
        if not monitors:
            # Fall back to the OS helper if we somehow can't
            # enumerate monitors directly.
            monitor = win32api.MonitorFromWindow(
                hwnd, win32con.MONITOR_DEFAULTTONEAREST
            )
            return win32api.GetMonitorInfo(monitor)["Monitor"]

        best_monitor = None
        best_area = 0

        for info in monitors:
            mrect = info["Monitor"]
            area = _intersection_area(win_rect, mrect)
            if area > best_area:
                best_area = area
                best_monitor = info

        if best_monitor is not None and best_area > 0:
            return best_monitor["Monitor"]

        # No intersection at all (window fully off-screen, minimised,
        # etc.) — fall back to nearest.
        cx = (wl + wr) // 2
        cy = (wt + wb) // 2
        monitor = win32api.MonitorFromPoint(
            (cx, cy), win32con.MONITOR_DEFAULTTONEAREST
        )
        return win32api.GetMonitorInfo(monitor)["Monitor"]

    except Exception:
        return None


# ============================================================
# BUILD SAFE RECTANGLE
# ============================================================

def build_safe_rect():
    global cached_rect, cached_rect_hwnd, cached_rect_border

    hwnd = get_target_hwnd()
    if not hwnd:
        return None

    if (
        cached_rect is not None
        and cached_rect_hwnd == hwnd
        and cached_rect_border == cursor_border
    ):
        return cached_rect

    monitor_rect = get_monitor_rect_from_window(hwnd)
    if not monitor_rect:
        return None

    left, top, right, bottom = monitor_rect

    left += cursor_border
    top += cursor_border
    right -= cursor_border
    bottom -= cursor_border

    if right <= left or bottom <= top:
        return None

    rect = (left, top, right, bottom)

    cached_rect = rect
    cached_rect_hwnd = hwnd
    cached_rect_border = cursor_border

    return rect


# ============================================================
# CURSOR CLIPPING
# ============================================================

def force_clip(rect):
    global cursor_locked, current_rect

    left, top, right, bottom = rect
    native_rect = wintypes.RECT(left, top, right, bottom)

    if user32.ClipCursor(ctypes.byref(native_rect)):
        cursor_locked = True
        current_rect = rect
        return True

    return False


def unlock_cursor():
    global cursor_locked, current_rect

    try:
        user32.ClipCursor(None)
    except Exception:
        pass

    cursor_locked = False
    current_rect = None


def get_cursor_position():
    point = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(point)):
        return (point.x, point.y)
    return None


def emergency_cursor_recovery():
    if not cursor_locked or current_rect is None:
        return

    position = get_cursor_position()
    if position is None:
        return

    x, y = position
    left, top, right, bottom = current_rect

    if left <= x < right and top <= y < bottom:
        return

    margin = 5

    new_x = max(left + margin, min(x, right - margin - 1))
    new_y = max(top + margin, min(y, bottom - margin - 1))

    user32.SetCursorPos(new_x, new_y)


# ============================================================
# PROCESS SCANNING
# ============================================================

def scan_processes():
    windows = enumerate_visible_windows()
    if not windows:
        return []

    groups = {}

    for pid in windows:
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            rss = proc.memory_info().rss
        except Exception:
            continue

        if not name:
            continue

        key = name.lower()

        group = groups.get(key)
        if group is None:
            groups[key] = {"name": name, "rss": rss, "pids": {pid}}
        else:
            group["rss"] += rss
            group["pids"].add(pid)

    return sorted(groups.values(), key=lambda g: g["rss"], reverse=True)


def find_pids_by_name(name):
    pids = set()
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = proc.info.get("name")
                if pname and pname.lower() == name.lower():
                    pids.add(proc.info["pid"])
            except Exception:
                continue
    except Exception:
        pass
    return pids


def enumerate_visible_windows():
    mapping = {}

    def callback(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                pid = get_window_process_id(hwnd)
                if pid and pid not in mapping:
                    mapping[pid] = hwnd
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        pass

    return mapping


# ============================================================
# WINDOW REGION + TASKBAR HELPERS
# ============================================================

def apply_rounded_window_region(hwnd, radius=14):
    try:
        rect = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))

        w = rect.right - rect.left
        h = rect.bottom - rect.top

        if w <= 0 or h <= 0:
            return

        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        user32.SetWindowRgn(wintypes.HWND(hwnd), rgn, True)
    except Exception:
        pass


def set_appwindow(window):
    try:
        hwnd = user32.GetParent(wintypes.HWND(window.winfo_id()))
        if not hwnd:
            hwnd = wintypes.HWND(window.winfo_id())

        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = style & ~WS_EX_TOOLWINDOW
        style = style | WS_EX_APPWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

        window.withdraw()
        window.after(10, window.deiconify)
    except Exception:
        pass


# ============================================================
# GUI STATUS
# ============================================================

def set_status(text, state):
    def update():
        try:
            status_label.config(text=text)

            if state == "running":
                color = SUCCESS
            elif state == "waiting":
                color = WARNING
            else:
                color = DANGER

            status_label.config(foreground=color)
            status_dot_canvas.itemconfig(dot_id, fill=color)

        except Exception:
            pass

    try:
        root.after(0, update)
    except Exception:
        pass


# ============================================================
# PANEL SWITCHING
# ============================================================

def show_list_panel():
    locked_frame.pack_forget()
    process_frame.pack(fill="both", expand=True)


def show_locked_panel(name):
    process_frame.pack_forget()

    locked_label.config(text=name)
    locked_frame.pack(fill="both", expand=True)


# ============================================================
# LIST POPULATION
# ============================================================

def populate_list(entries):
    global current_entries

    if not list_visible:
        return

    prev_sel = None
    try:
        sel = process_tree.selection()
        if sel:
            prev_sel = sel[0]
    except Exception:
        prev_sel = None

    current_entries = entries

    try:
        for item in process_tree.get_children():
            process_tree.delete(item)
    except Exception:
        return

    for entry in entries:
        mb = entry["rss"] / (1024.0 * 1024.0)
        iid = entry["name"]

        if process_tree.exists(iid):
            continue

        process_tree.insert(
            "", "end",
            iid=iid,
            values=(entry["name"], f"{mb:,.1f} MB")
        )

    if prev_sel and process_tree.exists(prev_sel):
        try:
            process_tree.selection_set(prev_sel)
            process_tree.see(prev_sel)
        except Exception:
            pass


# ============================================================
# START / STOP / EXIT
# ============================================================

def start_lock():
    global enabled
    global target_pids, target_name
    global last_foreground_state, list_visible

    if enabled:
        return

    try:
        sel = process_tree.selection()
    except Exception:
        sel = ()

    if not sel:
        set_status("Select a program from the list first", "stopped")
        return

    name = sel[0]

    entry = None
    for e in current_entries:
        if e["name"] == name:
            entry = e
            break

    if entry is None:
        set_status("List refreshing — try again in a moment", "stopped")
        return

    pids = find_pids_by_name(name)
    if not pids:
        pids = set(entry["pids"])

    if not pids:
        set_status("Could not resolve that program", "stopped")
        return

    target_name = name
    target_pids = pids

    invalidate_window_cache()

    enabled = True
    list_visible = False
    last_foreground_state = None

    show_locked_panel(name)

    start_button.set_enabled(False)
    stop_button.set_enabled(True)

    set_status(f"Running — waiting for {name}", "waiting")


def stop_lock():
    global enabled
    global target_pids, target_name
    global last_foreground_state, list_visible

    enabled = False

    last_foreground_state = None
    list_visible = True

    unlock_cursor()

    target_pids = set()
    target_name = ""

    invalidate_window_cache()

    show_list_panel()

    start_button.set_enabled(True)
    stop_button.set_enabled(False)

    set_status("Stopped — select a program", "stopped")

    refresh_event.set()


def exit_program():
    global running, enabled, list_visible

    running = False
    enabled = False
    list_visible = False

    refresh_event.set()

    unlock_cursor()

    try:
        root.destroy()
    except Exception:
        pass


# ============================================================
# BORDER SLIDER
# ============================================================

def on_border_change(value):
    global cursor_border

    try:
        cursor_border = int(value)
    except Exception:
        return

    try:
        border_value_label.config(text=f"{cursor_border} px")
    except Exception:
        pass


# ============================================================
# HIGH-SPEED WORKER
# ============================================================

def worker():
    global last_foreground_state

    while running:

        try:
            if not enabled:
                if cursor_locked:
                    unlock_cursor()
                time.sleep(0.01)
                continue

            focused = is_target_foreground()

            if focused:
                rect = build_safe_rect()

                if rect:
                    force_clip(rect)
                    emergency_cursor_recovery()

                if last_foreground_state is not True:
                    set_status(
                        f"{target_name} focused — HARD LOCK ACTIVE",
                        "running"
                    )

                last_foreground_state = True

            else:
                if cursor_locked:
                    unlock_cursor()

                if last_foreground_state is not False:
                    set_status(
                        f"Running — waiting for {target_name}",
                        "waiting"
                    )

                last_foreground_state = False

        except Exception:
            unlock_cursor()

        time.sleep(CHECK_INTERVAL)


# ============================================================
# LIST REFRESH THREAD
# ============================================================

def list_refresh_loop():
    while running:

        try:
            if list_visible:
                entries = scan_processes()

                if running and list_visible:
                    try:
                        root.after(0, lambda e=entries: populate_list(e))
                    except Exception:
                        pass

        except Exception:
            pass

        refresh_event.wait(LIST_REFRESH_INTERVAL)
        refresh_event.clear()


# ============================================================
# GUI
# ============================================================

root = tk.Tk()

# Taller window so nothing gets pushed off the bottom.
root.title("Cursor Lock")
root.geometry("520x780")
root.resizable(False, False)
root.configure(bg=BG_MAIN)

root.overrideredirect(True)

root.protocol("WM_DELETE_WINDOW", exit_program)


# ------------------------------------------------------------
# TTK THEME
# ------------------------------------------------------------

style = ttk.Style()
try:
    style.theme_use("clam")
except Exception:
    pass

style.layout("Stone.Treeview", [
    ("Treeview.treearea", {"sticky": "nswe"})
])

style.configure(
    "Stone.Treeview",
    background=BG_INPUT,
    fieldbackground=BG_INPUT,
    foreground=FG_PRIMARY,
    borderwidth=0,
    relief="flat",
    rowheight=32,
    font=("Segoe UI", 10),
)

style.configure(
    "Stone.Treeview.Heading",
    background=BG_CARD,
    foreground=FG_SECONDARY,
    borderwidth=0,
    relief="flat",
    font=("Segoe UI Semibold", 9),
)

style.map(
    "Stone.Treeview",
    background=[("selected", SELECT_BG)],
    foreground=[("selected", FG_PRIMARY)],
)

style.map(
    "Stone.Treeview.Heading",
    background=[("active", BG_CARD)],
    foreground=[("active", FG_SECONDARY)],
)


# ============================================================
# CUSTOM WIDGETS
# ============================================================

class RoundedButton(tk.Canvas):
    def __init__(
        self,
        parent,
        text,
        command,
        width=140,
        height=44,
        radius=10,
        bg=ACCENT,
        fg=BG_WINDOW,
        hover_bg=ACCENT_HOVER,
        disabled_bg=BTN_DISABLED_BG,
        disabled_fg=BTN_DISABLED_FG,
        font=("Segoe UI Semibold", 10),
        bg_outer=BG_MAIN,
        **kwargs
    ):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=bg_outer,
            highlightthickness=0,
            bd=0,
            **kwargs
        )

        self._btn_w = width
        self._btn_h = height

        self._text = text
        self._command = command
        self._radius = radius
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg
        self._disabled_bg = disabled_bg
        self._disabled_fg = disabled_fg
        self._font = font

        self._enabled = True
        self._hovering = False

        self._draw()

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.config(cursor="hand2")

    def _draw(self):
        self.delete("all")

        if not self._enabled:
            fill = self._disabled_bg
            fg = self._disabled_fg
        elif self._hovering:
            fill = self._hover_bg
            fg = self._fg
        else:
            fill = self._bg
            fg = self._fg

        draw_rounded_rect(
            self, 0, 0, self._btn_w, self._btn_h, self._radius,
            fill=fill, outline=""
        )
        self.create_text(
            self._btn_w / 2, self._btn_h / 2,
            text=self._text, fill=fg, font=self._font
        )

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.config(cursor="hand2" if self._enabled else "")
        self._draw()

    def _on_enter(self, _e):
        self._hovering = True
        self._draw()

    def _on_leave(self, _e):
        self._hovering = False
        self._draw()

    def _on_click(self, _e):
        if self._enabled and self._command:
            self._command()


class CloseButton(tk.Canvas):
    def __init__(self, parent, command, size=26, bg_outer=BG_MAIN, **kwargs):
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=bg_outer,
            highlightthickness=0,
            bd=0,
            **kwargs
        )

        self.command = command
        self._size = size
        self._hovering = False

        self._draw()

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.config(cursor="hand2")

    def _draw(self):
        self.delete("all")

        s = self._size

        if self._hovering:
            fill = "#3a2020"
            color = "#f87171"
        else:
            fill = None
            color = FG_MUTED

        if fill:
            draw_rounded_rect(self, 0, 0, s, s, 6, fill=fill, outline="")

        pad = 8
        self.create_line(
            pad, pad, s - pad, s - pad,
            fill=color, width=2, capstyle="round"
        )
        self.create_line(
            s - pad, pad, pad, s - pad,
            fill=color, width=2, capstyle="round"
        )

    def _on_enter(self, _e):
        self._hovering = True
        self._draw()

    def _on_leave(self, _e):
        self._hovering = False
        self._draw()

    def _on_click(self, _e):
        self.command()
        return "break"


class ThinScrollbar(tk.Canvas):
    def __init__(self, parent, command=None, width=8, bg_outer=BG_INPUT, **kwargs):
        super().__init__(
            parent,
            width=width,
            highlightthickness=0,
            bd=0,
            bg=bg_outer,
            **kwargs
        )

        self.command = command
        self._first = 0.0
        self._last = 1.0
        self._hovering = False
        self._thumb_bounds = (0, 0)
        self._drag_start_y = 0
        self._drag_start_frac = 0.0
        self._dragging = False

        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set(self, first, last):
        try:
            self._first = float(first)
            self._last = float(last)
        except Exception:
            return
        self._redraw()

    def _set_hover(self, h):
        self._hovering = h
        self._redraw()

    def _redraw(self, _event=None):
        self.delete("all")

        w = self.winfo_width()
        h = self.winfo_height()

        if h <= 1 or w <= 1:
            return

        thumb_top = int(self._first * h)
        thumb_bottom = int(self._last * h)

        min_h = 28
        if thumb_bottom - thumb_top < min_h:
            thumb_bottom = min(thumb_top + min_h, h)
            thumb_top = max(0, thumb_bottom - min_h)

        self._thumb_bounds = (thumb_top, thumb_bottom)

        color = SCROLL_HOVER if self._hovering else SCROLL_THUMB

        r = w // 2

        draw_rounded_rect(
            self,
            0, thumb_top,
            w, thumb_bottom,
            r,
            fill=color,
            outline=""
        )

    def _on_press(self, event):
        top, bottom = self._thumb_bounds
        h = self.winfo_height()

        if top <= event.y <= bottom:
            self._dragging = True
            self._drag_start_y = event.y
            self._drag_start_frac = self._first
        else:
            frac = max(0.0, min(1.0, event.y / max(1, h)))
            if self.command:
                self.command("moveto", frac)

    def _on_drag(self, event):
        if not self._dragging:
            return

        h = self.winfo_height()
        if h <= 0:
            return

        delta = (event.y - self._drag_start_y) / h
        frac = max(0.0, min(1.0, self._drag_start_frac + delta))

        if self.command:
            self.command("moveto", frac)

    def _on_release(self, _event):
        self._dragging = False


class RoundedSlider(tk.Canvas):
    def __init__(
        self,
        parent,
        min_val=5,
        max_val=50,
        value=20,
        command=None,
        width=440,
        height=44,
        bg_outer=BG_CARD,
        **kwargs
    ):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=bg_outer,
            highlightthickness=0,
            bd=0,
            **kwargs
        )

        self.min_val = min_val
        self.max_val = max_val
        self._value = value
        self._command = command
        self._dragging = False
        self._hovering = False
        self._thumb_cx = 0

        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.config(cursor="hand2")

    def get(self):
        return self._value

    def set(self, value):
        try:
            value = int(value)
        except Exception:
            return
        self._value = max(self.min_val, min(self.max_val, value))
        self._redraw()

    def _on_enter(self, _e):
        self._hovering = True
        self._redraw()

    def _on_leave(self, _e):
        self._hovering = False
        self._redraw()

    def _redraw(self, _event=None):
        self.delete("all")

        w = self.winfo_width()
        h = self.winfo_height()

        if w <= 1 or h <= 1:
            return

        track_h = 8
        track_y = h // 2
        track_top = track_y - track_h // 2
        track_bot = track_y + track_h // 2

        pad = 14

        draw_rounded_rect(
            self, pad, track_top, w - pad, track_bot,
            track_h // 2,
            fill=SLIDER_TRACK,
            outline=""
        )

        frac = (self._value - self.min_val) / max(
            1, (self.max_val - self.min_val)
        )
        thumb_cx = pad + frac * (w - 2 * pad)
        self._thumb_cx = thumb_cx

        if thumb_cx > pad + track_h:
            draw_rounded_rect(
                self, pad, track_top, thumb_cx, track_bot,
                track_h // 2,
                fill=SLIDER_FILL,
                outline=""
            )

        thumb_r = 11
        ring_r = thumb_r + 3

        self.create_oval(
            thumb_cx - ring_r, track_y - ring_r,
            thumb_cx + ring_r, track_y + ring_r,
            fill=SLIDER_THUMB_RING,
            outline=""
        )

        self.create_oval(
            thumb_cx - thumb_r, track_y - thumb_r,
            thumb_cx + thumb_r, track_y + thumb_r,
            fill=SLIDER_THUMB,
            outline=""
        )

        inner_r = 4
        self.create_oval(
            thumb_cx - inner_r, track_y - inner_r,
            thumb_cx + inner_r, track_y + inner_r,
            fill=SLIDER_TRACK,
            outline=""
        )

    def _on_click(self, event):
        self._dragging = True
        self._update_from_x(event.x)

    def _on_drag(self, event):
        if self._dragging:
            self._update_from_x(event.x)

    def _on_release(self, _event):
        self._dragging = False

    def _update_from_x(self, x):
        w = self.winfo_width()
        pad = 14

        frac = (x - pad) / max(1, (w - 2 * pad))
        frac = max(0.0, min(1.0, frac))

        val = int(round(
            self.min_val + frac * (self.max_val - self.min_val)
        ))

        if val != self._value:
            self._value = val
            self._redraw()
            if self._command:
                self._command(val)


# ============================================================
# ROUNDED CARD
# ============================================================

class RoundedCard(tk.Frame):
    """
    A rounded card whose inner content frame is PACKED (not placed)
    so its children's sizes propagate up and the card gets a real
    natural height instead of collapsing to zero pixels.
    """

    def __init__(
        self,
        parent,
        radius=12,
        fill=BG_CARD,
        outer=BG_MAIN,
        pad=1,
        height=None,
        **kwargs
    ):
        frame_kwargs = {"bg": outer}
        if height is not None:
            frame_kwargs["height"] = height

        super().__init__(parent, **frame_kwargs, **kwargs)

        self._radius = radius
        self._fill = fill
        self._outer = outer
        self._pad = pad

        self._bg_canvas = tk.Canvas(
            self, bg=outer, highlightthickness=0, bd=0
        )
        self._bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)

        self.content = tk.Frame(self, bg=fill)
        self.content.pack(fill="both", expand=True, padx=pad, pady=pad)

        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None):
        w = self.winfo_width()
        h = self.winfo_height()

        self._bg_canvas.delete("all")

        if w <= 1 or h <= 1:
            return

        draw_rounded_rect(
            self._bg_canvas,
            0, 0, w, h,
            self._radius,
            fill=self._fill,
            outline=""
        )


# ============================================================
# LAYOUT
# ============================================================

# ------------------------------------------------------------
# DRAG BAR
# ------------------------------------------------------------

drag_bar = tk.Frame(root, bg=BG_MAIN, height=34, cursor="fleur")
drag_bar.pack(fill="x", side="top")
drag_bar.pack_propagate(False)

drag_offset = {"x": 0, "y": 0}


def _drag_start(event):
    drag_offset["x"] = event.x_root - root.winfo_x()
    drag_offset["y"] = event.y_root - root.winfo_y()


def _drag_motion(event):
    x = event.x_root - drag_offset["x"]
    y = event.y_root - drag_offset["y"]
    root.geometry(f"+{x}+{y}")


drag_bar.bind("<Button-1>", _drag_start)
drag_bar.bind("<B1-Motion>", _drag_motion)


close_btn = CloseButton(drag_bar, command=exit_program, size=26, bg_outer=BG_MAIN)
close_btn.pack(side="right", padx=(0, 8), pady=4)


# ------------------------------------------------------------
# TITLE BLOCK
# ------------------------------------------------------------

title_block = tk.Frame(root, bg=BG_MAIN, cursor="hand2")
title_block.pack(fill="x", padx=24, pady=(0, 14))

title_label = tk.Label(
    title_block,
    text="Cursor Lock",
    font=("Segoe UI", 22, "bold"),
    bg=BG_MAIN,
    fg=FG_PRIMARY,
    anchor="w",
    cursor="hand2",
)
title_label.pack(anchor="w")

subtitle_label = tk.Label(
    title_block,
    text="by @itsmeprinceyt",
    font=("Segoe UI", 10),
    bg=BG_MAIN,
    fg=FG_SECONDARY,
    anchor="w",
    cursor="hand2",
)
subtitle_label.pack(anchor="w")


def _open_website(_event=None):
    try:
        webbrowser.open(WEBSITE_URL)
    except Exception:
        pass


def _title_hover_in(_event=None):
    title_label.config(fg=ACCENT)
    subtitle_label.config(fg=ACCENT)


def _title_hover_out(_event=None):
    title_label.config(fg=FG_PRIMARY)
    subtitle_label.config(fg=FG_SECONDARY)


for w in (title_block, title_label, subtitle_label):
    w.bind("<Button-1>", _open_website)
    w.bind("<Enter>", _title_hover_in)
    w.bind("<Leave>", _title_hover_out)


# ------------------------------------------------------------
# CONTENT AREA
# ------------------------------------------------------------

content_area = tk.Frame(root, bg=BG_MAIN)
content_area.pack(fill="both", expand=True, padx=0, pady=0)


# ---------- PROCESS LIST CARD ----------

list_card = RoundedCard(
    content_area, radius=14, fill=BG_CARD, outer=BG_MAIN, pad=1
)
list_card.pack(fill="x", padx=20, pady=(0, 10))

list_inner = list_card.content

list_header = tk.Label(
    list_inner,
    text="Running Programs",
    font=("Segoe UI Semibold", 11),
    bg=BG_CARD,
    fg=FG_PRIMARY,
    anchor="w",
)
list_header.pack(anchor="w", padx=16, pady=(14, 0))

list_subheader = tk.Label(
    list_inner,
    text="sorted by RAM · refreshes every 10s",
    font=("Segoe UI", 8),
    bg=BG_CARD,
    fg=FG_MUTED,
    anchor="w",
)
list_subheader.pack(anchor="w", padx=16, pady=(0, 10))


process_frame = tk.Frame(list_inner, bg=BG_CARD)

list_wrap_outer = tk.Frame(process_frame, bg=BG_INPUT)
list_wrap_outer.pack(fill="both", expand=True, padx=14, pady=(0, 14))

process_tree = ttk.Treeview(
    list_wrap_outer,
    columns=("name", "ram"),
    show="headings",
    style="Stone.Treeview",
    selectmode="browse",
    height=8,
)

process_tree.heading("name", text="Process", anchor="w")
process_tree.heading("ram", text="Memory", anchor="e")

process_tree.column("name", width=310, anchor="w", stretch=True)
process_tree.column("ram", width=100, anchor="e", stretch=False)

custom_scroll = ThinScrollbar(
    list_wrap_outer,
    command=process_tree.yview,
    width=8,
    bg_outer=BG_INPUT,
)

process_tree.configure(yscrollcommand=custom_scroll.set)

process_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
custom_scroll.pack(side="right", fill="y", padx=(4, 4), pady=6)

process_tree.bind("<Double-Button-1>", lambda _e: start_lock())


# ---------- LOCKED PANEL ----------

locked_frame = tk.Frame(list_inner, bg=BG_CARD)

locked_icon = tk.Label(
    locked_frame,
    text="🔒",
    font=("Segoe UI Emoji", 30),
    bg=BG_CARD,
    fg=FG_SECONDARY,
)
locked_icon.pack(pady=(40, 10))

locked_caption = tk.Label(
    locked_frame,
    text="Currently locked to",
    font=("Segoe UI", 9),
    bg=BG_CARD,
    fg=FG_MUTED,
)
locked_caption.pack()

locked_label = tk.Label(
    locked_frame,
    text="",
    font=("Segoe UI Semibold", 15),
    bg=BG_CARD,
    fg=FG_PRIMARY,
)
locked_label.pack(pady=(4, 14))

locked_hint = tk.Label(
    locked_frame,
    text="The cursor is confined to this program's monitor.\n"
         "Press STOP to release the cursor and pick another program.",
    font=("Segoe UI", 9),
    bg=BG_CARD,
    fg=FG_SECONDARY,
    justify="center",
)
locked_hint.pack(pady=(0, 40))


# ---------- SAFETY BORDER CARD ----------

border_card = RoundedCard(
    content_area,
    radius=14,
    fill=BG_CARD,
    outer=BG_MAIN,
    pad=1,
    height=150,
)
border_card.pack(fill="x", padx=20, pady=(0, 10))
border_card.pack_propagate(False)

border_inner = border_card.content

border_top_row = tk.Frame(border_inner, bg=BG_CARD)
border_top_row.pack(fill="x", padx=16, pady=(14, 0))

border_caption = tk.Label(
    border_top_row,
    text="Safety Border",
    font=("Segoe UI Semibold", 10),
    bg=BG_CARD,
    fg=FG_PRIMARY,
)
border_caption.pack(side="left")

border_value_label = tk.Label(
    border_top_row,
    text=f"{DEFAULT_CURSOR_BORDER} px",
    font=("Segoe UI Semibold", 12),
    bg=BG_CARD,
    fg=ACCENT,
)
border_value_label.pack(side="right")


border_slider = RoundedSlider(
    border_inner,
    min_val=5,
    max_val=50,
    value=DEFAULT_CURSOR_BORDER,
    command=on_border_change,
    width=440,
    height=48,
    bg_outer=BG_CARD,
)

border_slider.pack(fill="x", padx=10, pady=(6, 0))

border_hint_row = tk.Frame(border_inner, bg=BG_CARD)
border_hint_row.pack(fill="x", padx=22, pady=(0, 10))

tk.Label(
    border_hint_row,
    text="5 px",
    font=("Segoe UI", 8),
    bg=BG_CARD,
    fg=FG_MUTED,
).pack(side="left")

tk.Label(
    border_hint_row,
    text="50 px",
    font=("Segoe UI", 8),
    bg=BG_CARD,
    fg=FG_MUTED,
).pack(side="right")


# ---------- STATUS ROW ----------

status_row = tk.Frame(root, bg=BG_MAIN)
status_row.pack(fill="x", padx=30, pady=(6, 4))

status_dot_canvas = tk.Canvas(
    status_row,
    width=14,
    height=14,
    bg=BG_MAIN,
    highlightthickness=0,
)
status_dot_canvas.pack(side="left", pady=(1, 0))

dot_id = status_dot_canvas.create_oval(
    2, 2, 12, 12,
    fill=DANGER,
    outline="",
)

status_label = tk.Label(
    status_row,
    text="Stopped — select a program",
    font=("Segoe UI", 10),
    bg=BG_MAIN,
    fg=DANGER,
    anchor="w",
)
status_label.pack(side="left", padx=(10, 0), fill="x", expand=True)


# ---------- BUTTON ROW ----------

button_row = tk.Frame(root, bg=BG_MAIN)
button_row.pack(pady=(10, 18))

start_button = RoundedButton(
    button_row,
    text="START",
    command=start_lock,
    width=140,
    height=44,
    radius=10,
    bg=ACCENT,
    fg=BG_WINDOW,
    hover_bg=ACCENT_HOVER,
    bg_outer=BG_MAIN,
)
start_button.grid(row=0, column=0, padx=5)

stop_button = RoundedButton(
    button_row,
    text="STOP",
    command=stop_lock,
    width=140,
    height=44,
    radius=10,
    bg=STOP_BG,
    fg=STOP_FG,
    hover_bg=STOP_BG_HOVER,
    disabled_bg=BTN_DISABLED_BG,
    disabled_fg=BTN_DISABLED_FG,
    bg_outer=BG_MAIN,
)
stop_button.set_enabled(False)
stop_button.grid(row=0, column=1, padx=5)

exit_button = RoundedButton(
    button_row,
    text="EXIT",
    command=exit_program,
    width=90,
    height=44,
    radius=10,
    bg=BTN_DARK,
    fg=FG_PRIMARY,
    hover_bg=BTN_DARK_HOVER,
    bg_outer=BG_MAIN,
)
exit_button.grid(row=0, column=2, padx=5)


# ------------------------------------------------------------
# INITIAL PANEL
# ------------------------------------------------------------

process_frame.pack(fill="both", expand=True)


# ============================================================
# ROUND WINDOW REGION + TASKBAR FIX
# ============================================================

def _apply_window_rounding():
    try:
        root.update_idletasks()

        hwnd = user32.GetParent(wintypes.HWND(root.winfo_id()))
        if not hwnd:
            hwnd = root.winfo_id()

        apply_rounded_window_region(int(hwnd), radius=14)
    except Exception:
        pass


root.after(30, _apply_window_rounding)
root.after(50, lambda: set_appwindow(root))


# ============================================================
# START THREADS
# ============================================================

worker_thread = threading.Thread(target=worker, daemon=True)
worker_thread.start()

scanner_thread = threading.Thread(target=list_refresh_loop, daemon=True)
scanner_thread.start()


# ============================================================
# MAIN LOOP
# ============================================================

root.mainloop()


# ============================================================
# FINAL CLEANUP
# ============================================================

running = False
enabled = False
list_visible = False

refresh_event.set()

unlock_cursor()