import ctypes
import ctypes.wintypes as wintypes
import threading
import time
import tkinter as tk
from tkinter import ttk

import win32api
import win32con
import win32gui
import win32process


# ============================================================
# CONFIGURATION
# ============================================================

PROCESS_NAME = "GenshinImpact.exe"

# ------------------------------------------------------------
# HARD SAFETY BORDER
# ------------------------------------------------------------
#
# The cursor is NEVER allowed to reach the actual monitor edge.
#
# 20px is intentionally conservative.
#
CURSOR_BORDER = 20


# ------------------------------------------------------------
# AGGRESSIVE CHECK INTERVAL
# ------------------------------------------------------------
#
# Target interval:
#
# 0.001 = 1ms
#
# Windows/Python scheduling means this is not guaranteed to
# execute exactly every 1ms, but it makes the loop much more
# aggressive than the previous 20ms version.
#
CHECK_INTERVAL = 0.001


# ============================================================
# WINDOWS API
# ============================================================

user32 = ctypes.WinDLL(
    "user32",
    use_last_error=True
)

kernel32 = ctypes.WinDLL(
    "kernel32",
    use_last_error=True
)


# ------------------------------------------------------------
# ClipCursor
# ------------------------------------------------------------

user32.ClipCursor.argtypes = [
    ctypes.POINTER(wintypes.RECT)
]

user32.ClipCursor.restype = wintypes.BOOL


# ------------------------------------------------------------
# GetCursorPos
# ------------------------------------------------------------

user32.GetCursorPos.argtypes = [
    ctypes.POINTER(wintypes.POINT)
]

user32.GetCursorPos.restype = wintypes.BOOL


# ------------------------------------------------------------
# SetCursorPos
# ------------------------------------------------------------

user32.SetCursorPos.argtypes = [
    ctypes.c_int,
    ctypes.c_int
]

user32.SetCursorPos.restype = wintypes.BOOL


# ============================================================
# GLOBAL STATE
# ============================================================

running = True

enabled = False

cursor_locked = False

current_rect = None

genshin_pid = None

last_foreground_state = None

worker_thread = None


# ============================================================
# PROCESS DETECTION
# ============================================================
def find_genshin_pid():
    """
    Find Genshin's PID by enumerating visible windows.
    """

    global genshin_pid

    found_pid = None

    def callback(hwnd, _):

        nonlocal found_pid

        if found_pid:
            return False

        if not win32gui.IsWindowVisible(hwnd):
            return True

        try:

            _, pid = win32process.GetWindowThreadProcessId(hwnd)

            if not pid:
                return True

            title = win32gui.GetWindowText(hwnd)

            # Genshin's main window normally has a title.
            if title:
                # Store the PID for the window.
                found_pid = pid

                # We still need to verify that this is actually
                # Genshin rather than another window.
                #
                # For that, check the executable through the
                # process API below.
                #

                try:

                    import psutil

                    process = psutil.Process(pid)

                    exe_name = process.name()

                    if exe_name.lower() == PROCESS_NAME.lower():
                        return False

                except Exception:

                    pass

                found_pid = None

        except Exception:

            pass

        return True

    try:

        win32gui.EnumWindows(
            callback,
            None
        )

    except Exception:

        pass

    genshin_pid = found_pid

    return found_pid

# ============================================================
# WINDOW HELPERS
# ============================================================

def get_window_process_id(hwnd):

    try:

        _, pid = win32process.GetWindowThreadProcessId(
            hwnd
        )

        return pid

    except Exception:

        return None


def find_genshin_window():

    global genshin_pid

    if not genshin_pid:

        find_genshin_pid()

    if not genshin_pid:
        return None

    result = None

    def callback(hwnd, _):

        nonlocal result

        if result:
            return False

        if not win32gui.IsWindowVisible(hwnd):
            return True

        pid = get_window_process_id(hwnd)

        if pid == genshin_pid:

            title = win32gui.GetWindowText(hwnd)

            if title:

                result = hwnd

                return False

        return True

    try:

        win32gui.EnumWindows(
            callback,
            None
        )

    except Exception:

        return None

    return result


# ============================================================
# FOREGROUND CHECK
# ============================================================

def is_genshin_foreground():

    global genshin_pid

    hwnd = win32gui.GetForegroundWindow()

    if not hwnd:
        return False

    pid = get_window_process_id(hwnd)

    if not pid:
        return False

    if genshin_pid == pid:

        return True

    # Genshin may have restarted.
    if not genshin_pid:

        find_genshin_pid()

    return genshin_pid == pid


# ============================================================
# MONITOR DETECTION
# ============================================================

def get_monitor_rect_from_window(hwnd):

    try:

        left, top, right, bottom = (
            win32gui.GetWindowRect(hwnd)
        )

        center_x = (
            left + right
        ) // 2

        center_y = (
            top + bottom
        ) // 2

        monitor = win32api.MonitorFromPoint(
            (
                center_x,
                center_y
            ),
            win32con.MONITOR_DEFAULTTONEAREST
        )

        info = win32api.GetMonitorInfo(
            monitor
        )

        return info["Monitor"]

    except Exception:

        return None


# ============================================================
# BUILD SAFE RECTANGLE
# ============================================================

def build_safe_rect():

    hwnd = find_genshin_window()

    if not hwnd:
        return None

    monitor_rect = get_monitor_rect_from_window(
        hwnd
    )

    if not monitor_rect:
        return None

    left, top, right, bottom = monitor_rect

    # --------------------------------------------------------
    # HARD SAFETY BORDER
    # --------------------------------------------------------

    left += CURSOR_BORDER
    top += CURSOR_BORDER

    right -= CURSOR_BORDER
    bottom -= CURSOR_BORDER

    if right <= left:
        return None

    if bottom <= top:
        return None

    return (
        left,
        top,
        right,
        bottom
    )


# ============================================================
# APPLY CURSOR CLIP
# ============================================================

def force_clip(rect):
    """
    Force Windows to restrict the cursor to rect.
    """

    global cursor_locked
    global current_rect

    left, top, right, bottom = rect

    native_rect = wintypes.RECT(
        left,
        top,
        right,
        bottom
    )

    success = user32.ClipCursor(
        ctypes.byref(native_rect)
    )

    if success:

        cursor_locked = True
        current_rect = rect

        return True

    return False


# ============================================================
# HARD LOCK
# ============================================================

def hard_lock():

    rect = build_safe_rect()

    if not rect:
        return False

    return force_clip(
        rect
    )


# ============================================================
# UNLOCK
# ============================================================

def unlock_cursor():

    global cursor_locked
    global current_rect

    try:

        user32.ClipCursor(
            None
        )

    except Exception:

        pass

    cursor_locked = False
    current_rect = None


# ============================================================
# DIRECT CURSOR CHECK
# ============================================================

def get_cursor_position():

    point = wintypes.POINT()

    if user32.GetCursorPos(
        ctypes.byref(point)
    ):

        return (
            point.x,
            point.y
        )

    return None


# ============================================================
# EMERGENCY CURSOR RECOVERY
# ============================================================

def emergency_cursor_recovery():

    if not cursor_locked:
        return

    if current_rect is None:
        return

    position = get_cursor_position()

    if position is None:
        return

    x, y = position

    left, top, right, bottom = current_rect

    # --------------------------------------------------------
    # Cursor is inside safe area.
    # --------------------------------------------------------

    if (
        left <= x < right
        and
        top <= y < bottom
    ):

        return

    # --------------------------------------------------------
    # Cursor escaped.
    #
    # Put it back immediately.
    # --------------------------------------------------------

    margin = 5

    new_x = max(
        left + margin,
        min(
            x,
            right - margin - 1
        )
    )

    new_y = max(
        top + margin,
        min(
            y,
            bottom - margin - 1
        )
    )

    user32.SetCursorPos(
        new_x,
        new_y
    )


# ============================================================
# GUI STATUS
# ============================================================

def set_status(
    text,
    state
):

    def update():

        try:

            status_label.config(
                text=text
            )

            if state == "running":

                status_label.config(
                    foreground="#00a83b"
                )

            elif state == "waiting":

                status_label.config(
                    foreground="#cc8500"
                )

            elif state == "stopped":

                status_label.config(
                    foreground="#d00000"
                )

        except Exception:

            pass

    try:

        root.after(
            0,
            update
        )

    except Exception:

        pass


# ============================================================
# START
# ============================================================

def start_lock():

    global enabled
    global genshin_pid
    global last_foreground_state

    if enabled:
        return

    # Refresh process information.
    genshin_pid = None

    find_genshin_pid()

    enabled = True

    last_foreground_state = None

    start_button.config(
        state=tk.DISABLED
    )

    stop_button.config(
        state=tk.NORMAL
    )

    set_status(
        "● Running — waiting for Genshin",
        "waiting"
    )


# ============================================================
# STOP
# ============================================================

def stop_lock():

    global enabled
    global last_foreground_state

    # Disable FIRST.
    enabled = False

    last_foreground_state = None

    # Immediately remove Windows clipping.
    unlock_cursor()

    start_button.config(
        state=tk.NORMAL
    )

    stop_button.config(
        state=tk.DISABLED
    )

    set_status(
        "● Stopped",
        "stopped"
    )


# ============================================================
# EXIT
# ============================================================

def exit_program():

    global running
    global enabled

    running = False
    enabled = False

    # Critical:
    #
    # Never leave ClipCursor active after exiting.
    #

    unlock_cursor()

    try:

        root.destroy()

    except Exception:

        pass


# ============================================================
# HIGH-SPEED WORKER
# ============================================================

def worker():

    global last_foreground_state

    while running:

        try:

            # ------------------------------------------------
            # Disabled
            # ------------------------------------------------

            if not enabled:

                if cursor_locked:

                    unlock_cursor()

                time.sleep(
                    0.01
                )

                continue

            # ------------------------------------------------
            # Check Genshin focus.
            # ------------------------------------------------

            focused = is_genshin_foreground()

            # =================================================
            # GENSHIN FOCUSED
            # =================================================

            if focused:

                # ------------------------------------------------
                # HARD RE-CLIP
                #
                # We intentionally re-apply ClipCursor.
                #
                # This means even if Genshin tries to modify
                # the cursor capture state, our program keeps
                # restoring the Windows restriction.
                # ------------------------------------------------

                rect = build_safe_rect()

                if rect:

                    force_clip(
                        rect
                    )

                    # ------------------------------------------------
                    # Emergency physical cursor recovery.
                    # ------------------------------------------------

                    emergency_cursor_recovery()

                if last_foreground_state is not True:

                    set_status(
                        "● Genshin detected — HARD LOCK ACTIVE",
                        "running"
                    )

                last_foreground_state = True

            # =================================================
            # GENSHIN NOT FOCUSED
            # =================================================

            else:

                if cursor_locked:

                    unlock_cursor()

                if last_foreground_state is not False:

                    set_status(
                        "● Running — waiting for Genshin",
                        "waiting"
                    )

                last_foreground_state = False

        except Exception:

            # Safety fallback.
            unlock_cursor()

        # ----------------------------------------------------
        # High-frequency loop.
        # ----------------------------------------------------

        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# GUI
# ============================================================

root = tk.Tk()

root.title(
    "Genshin Mouse Lock"
)

root.geometry(
    "410x285"
)

root.resizable(
    False,
    False
)

root.protocol(
    "WM_DELETE_WINDOW",
    exit_program
)


# ============================================================
# TITLE
# ============================================================

title_label = tk.Label(
    root,
    text="Genshin Mouse Lock",
    font=(
        "Segoe UI",
        17,
        "bold"
    )
)

title_label.pack(
    pady=(20, 5)
)


# ============================================================
# DESCRIPTION
# ============================================================

description_label = tk.Label(
    root,
    text="Hard-lock the cursor inside Genshin's monitor",
    font=(
        "Segoe UI",
        9
    )
)

description_label.pack(
    pady=(0, 10)
)


# ============================================================
# SAFETY BORDER
# ============================================================

border_label = tk.Label(
    root,
    text=f"Safety border: {CURSOR_BORDER}px",
    font=(
        "Segoe UI",
        9
    )
)

border_label.pack(
    pady=(0, 8)
)


# ============================================================
# STATUS
# ============================================================

status_label = tk.Label(
    root,
    text="● Stopped",
    font=(
        "Segoe UI",
        10,
        "bold"
    ),
    foreground="#d00000"
)

status_label.pack(
    pady=(0, 15)
)


# ============================================================
# BUTTON FRAME
# ============================================================

button_frame = tk.Frame(
    root
)

button_frame.pack()


# ============================================================
# START
# ============================================================

start_button = ttk.Button(
    button_frame,
    text="START",
    command=start_lock,
    width=14
)

start_button.grid(
    row=0,
    column=0,
    padx=5
)


# ============================================================
# STOP
# ============================================================

stop_button = ttk.Button(
    button_frame,
    text="STOP",
    command=stop_lock,
    width=14,
    state=tk.DISABLED
)

stop_button.grid(
    row=0,
    column=1,
    padx=5
)


# ============================================================
# EXIT
# ============================================================

exit_button = ttk.Button(
    root,
    text="EXIT",
    command=exit_program,
    width=30
)

exit_button.pack(
    pady=(18, 0)
)


# ============================================================
# FOOTER
# ============================================================

footer_label = tk.Label(
    root,
    text="START → launch Genshin → play normally",
    font=(
        "Segoe UI",
        8
    ),
    foreground="#777777"
)

footer_label.pack(
    pady=(12, 0)
)


# ============================================================
# START WORKER
# ============================================================

worker_thread = threading.Thread(
    target=worker,
    daemon=True
)

worker_thread.start()


# ============================================================
# START GUI
# ============================================================

root.mainloop()


# ============================================================
# FINAL SAFETY CLEANUP
# ============================================================

running = False

enabled = False

unlock_cursor()