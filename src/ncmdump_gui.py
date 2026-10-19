# -*- coding: utf-8 -*-
"""毛玻璃 GUI：把网易云 .ncm 批量转成本地可播放的 FLAC/MP3。

设计要点
  * 使用**系统默认窗口**（保留 Windows 标题栏、可拉伸边框、Aero Snap），窗口材质交给
    pywinstyles 驱动系统合成器；不支持时依次回退到 DWM 材质、纯色面板，界面始终可用。
  * 转换跑在线程池里，且**不开子进程**：打包成 EXE 后多进程容易踩坑，而 XOR 与
    文件 I/O 都已交给 numpy / 系统调用，线程足够跑满磁盘。
  * 每完成一首歌就把结果回传主线程，进度条、日志、统计实时刷新。
  * 删除原文件前仍走 ncmdump 的逐字节校验。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue

# Usable both as a module (``python -m ncmdump_gui``) and as a plain script
# (``python src/ncmdump_gui.py``), which is how PyInstaller entry points usually run it.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from ncmdump.metadata import DEVELOPER, RELEASES_URL, REPO_URL, VERSION
except ImportError:                       # pragma: no cover - only before build/install
    DEVELOPER, REPO_URL, RELEASES_URL, VERSION = ("Calvin Vollerei Studio", "", "", "0.0.0")

# --- window material --------------------------------------------------------
#
# pywinstyles drives the Windows composition APIs for us: "acrylic" paints the background
# transparent, extends the DWM frame into the whole client area and turns on the blurred
# backdrop — so the blur covers the entire window, corners included, instead of only an
# inner panel.

try:
    import pywinstyles as _pws
except Exception:                     # pragma: no cover - dependency is optional
    _pws = None

# The window material is fixed at build time: a single material, so there is no switching
# UI and no unused style code to ship. To ship a different one, change DEFAULT_MATERIAL
# (one line) or set NCM_GLASS at runtime for a look-see:
# acrylic | mica | aero | solid.
DEFAULT_MATERIAL = "aero"
WINDOW_MATERIALS = ("acrylic", "mica", "aero", "solid")
# alpha of the tint painted over the OS material: lower = more transparent
GLASS_TINT_ALPHA = 46


def apply_window_style(window, mode: str) -> str:
    """Apply a pywinstyles style to a Qt window. Returns the mode actually used.

    Pass the **widget**, never its ``int(winId())``: pywinstyles' ``paint()`` helper
    dispatches on the window type, and an integer hits none of its branches — it then
    prints ``Don't know what the window type is, please paint it black`` and never makes
    the client area transparent (see docs/GLASS-TROUBLESHOOTING.md, pit 5).

    ``paint()`` also replaces the widget stylesheet, so the caller must re-apply the real
    stylesheet *after* this returns.
    """
    if _pws is None or os.name != "nt":
        return "none"
    if mode == "solid":
        _pws.apply_style(window, "normal")
        return "solid"
    try:
        style = mode if mode in ("acrylic", "mica", "aero") else "acrylic"
        _pws.apply_style(window, style)
        return style
    except Exception as exc:
        _boot_log("pywinstyles failed (%s): %s" % (mode, exc))
        return "none"


def window_stylesheet(glass_on: bool, base: str = "") -> str:
    """Global styling plus a rule that keeps the window's own background transparent.

    The extra rule is *appended* to the active theme sheet rather than set on its own:
    ``setStyleSheet`` replaces, and a window-level sheet outranks the application one, so
    hard-coding a base here silently pins the window to that theme no matter what the user
    picked.
    """
    extra = ""
    if glass_on:
        extra = "\nQWidget#root { background: transparent; }\n"
    return (base or STYLE) + extra


DWMWA_BORDER_COLOR = 34
_DWMWA_COLOR_NONE = 0xFFFFFFFE      # DWMWA_COLOR_NONE


def pywinstyles_normal(hwnd: int) -> None:
    """Reset the window to the default (non-glass) look."""
    if _pws is None or os.name != "nt":
        return
    try:
        _pws.apply_style(hwnd, "normal")
    except Exception as exc:
        _boot_log("reset style failed: %s" % exc)


def tune_native_frame(hwnd: int, glass_on: bool = True,
                      header: str = "#141a28", border: str = "#20293d",
                      title: str = "#e3ebff") -> None:
    """Colour the native title bar and border so they match the app, and drop the border
    frame when the glass material is on (the DWM material already defines the edge)."""
    if _pws is None or os.name != "nt":
        return
    try:
        _pws.change_header_color(hwnd, header)
    except Exception as exc:
        _boot_log("header colour failed: %s" % exc)
    try:
        _pws.change_title_color(hwnd, title)
    except Exception:
        pass
    try:
        if glass_on:
            _dwm_set(hwnd, DWMWA_BORDER_COLOR, _DWMWA_COLOR_NONE)
        else:
            _pws.change_border_color(hwnd, border)
    except Exception as exc:
        _boot_log("border colour failed: %s" % exc)


# --- Win32 glass ------------------------------------------------------------
#
# Windows 11 exposes a documented system backdrop (DWMWA_SYSTEMBACKDROP_TYPE); the older
# SetWindowCompositionAttribute acrylic is undocumented and, on Win11, renders as a flat
# dark tint that an opaque card easily hides. So: try the documented API first, then fall
# back, then fall back again. Whichever one sticks is reported back for the UI to show.

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_MAINWINDOW = 2      # "Mica"
DWMSBT_TRANSIENTWINDOW = 3  # "Acrylic"
DWMWA_CAPTION_COLOR = 35

# --- Win32 window style bits -------------------------------------------------
#
# Qt's FramelessWindowHint turns the window into a plain WS_POPUP. The shell then stops
# treating it as an application window at all: no Aero Snap, no drag-to-edge tiling, no
# Win+arrow. Restoring WS_THICKFRAME / WS_MAXIMIZEBOX puts it back on the shell's radar;
# WM_NCCALCSIZE is then answered in nativeEvent so the 7 px border the frame style implies
# never shows up in the client area.
GWL_STYLE = -16
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
WS_SYSMENU = 0x00080000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084


def _dwm_set(hwnd, attr, value):
    import ctypes
    from ctypes import wintypes

    ptr = ctypes.c_int(value)
    res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
        wintypes.HWND(hwnd), ctypes.c_int(attr), ctypes.byref(ptr), ctypes.sizeof(ptr))
    return res == 0


def _accent(hwnd, state, tint):
    """Undocumented Win10/11 acrylic path, used only when DWM refuses."""
    import ctypes
    from ctypes import wintypes

    class ACCENTPOLICY(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

    class WINCOMPATTRDATA(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                    ("SizeOfData", ctypes.c_size_t)]

    accent = ACCENTPOLICY()
    accent.AccentState = state
    accent.AccentFlags = 2
    r, g, b, a = tint
    accent.GradientColor = (a << 24) | (b << 16) | (g << 8) | r
    data = WINCOMPATTRDATA()
    data.Attribute = 19  # WCA_ACCENT_POLICY
    data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
    data.SizeOfData = ctypes.sizeof(accent)
    fn = ctypes.windll.user32.SetWindowCompositionAttribute
    fn.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
    fn.restype = ctypes.c_int
    return bool(fn(wintypes.HWND(hwnd), ctypes.byref(data)))


def apply_glass(hwnd: int, enabled: bool = True, prefer: str = "") -> str:
    """Turn the OS backdrop on/off. Returns a short label describing what took effect.

    ``prefer`` can pin the material ("acrylic", "mica", "legacy") which is useful for
    checking on a machine where one of them misbehaves.
    """
    if os.name != "nt":
        return "none"
    try:
        import ctypes  # noqa: F401  (exercises that the DLL layer is reachable)
    except Exception:
        return "none"
    try:
        if not enabled:
            _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, 0)          # DWMSBT_AUTO / none
            _accent(hwnd, 0, (0, 0, 0, 0))                        # ACCENT_DISABLED
            return "off"
        # Turn OFF the OS corner rounding: this window paints its own 16 px rounded pane,
        # and a second, differently-sized OS rounding would shave slivers off the corners.
        try:
            _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, 1)     # DWMWCP_DONOTROUND
        except Exception:
            pass

        if prefer != "legacy":
            order = ([DWMSBT_TRANSIENTWINDOW, DWMSBT_MAINWINDOW] if prefer != "mica"
                     else [DWMSBT_MAINWINDOW, DWMSBT_TRANSIENTWINDOW])
            names = {DWMSBT_TRANSIENTWINDOW: "win11-acrylic", DWMSBT_MAINWINDOW: "win11-mica"}
            for kind in order:
                if _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, kind):
                    return names[kind]
        if prefer != "mica" and prefer != "acrylic":
            if _accent(hwnd, 4, (16, 18, 26, 165)):               # legacy ACRYLICBLURBEHIND
                return "acrylic"
            if _accent(hwnd, 3, (0, 0, 0, 0)):                    # legacy BLURBEHIND
                return "blur"
    except Exception:
        pass
    return "none"


# NOTE: this used to call DwmExtendFrameIntoClientArea(hwnd, MARGINS(1,1,1,1)) to get a
# shadow on the frameless window. It is deliberately gone: that call also paints a 1 px
# accent border around the whole window, which is the "extra ring" that showed up on
# screen. Restoring WS_THICKFRAME (see MainWindow._enable_snap) makes DWM supply the
# normal window shadow instead, with no border of its own.


# --- Qt ---------------------------------------------------------------------

from PySide6.QtCore import QLocale, QRectF, QStandardPaths, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def make_icon(size: int = 128) -> QIcon:
    """Draw the app icon so the build needs no image assets."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor(88, 190, 255))
    grad.setColorAt(1.0, QColor(150, 110, 255))
    painter.setBrush(grad)
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, size, size, size * 0.24, size * 0.24)
    painter.setPen(QColor(255, 255, 255, 240))
    font = QFont("Segoe UI", int(size * 0.28), QFont.Bold)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignCenter, "NCM")
    painter.end()
    return QIcon(pix)


STYLE = """
QWidget { color: #eef3ff; font-family: "Microsoft YaHei", "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; font-weight: 700; }
#root { background: transparent; }
/* ---- main glass card: one continuous sheet, edges only where groups need them ---- */
#card {
    background: rgba(14, 19, 34, 26);
    border: none;
    border-radius: 22px;
}
#card[solid="true"] {
    background: rgba(20, 24, 38, 246);
}
/* ---- tinted glass panels: one colour per functional group, all very translucent ---- */
#panel {
    background: rgba(255, 255, 255, 8);
    border: 1px solid rgba(255, 255, 255, 22);
    border-radius: 16px;
}
#panelOut {
    background: rgba(86, 160, 255, 24);
    border: 1px solid rgba(140, 190, 255, 44);
    border-radius: 16px;
}
#panelOpt {
    background: rgba(46, 226, 168, 20);
    border: 1px solid rgba(120, 240, 200, 40);
    border-radius: 16px;
}
#panelRun {
    background: rgba(158, 118, 255, 24);
    border: 1px solid rgba(190, 160, 255, 44);
    border-radius: 16px;
}
QPushButton#menu {
    background: rgba(255, 255, 255, 22);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 9px; padding: 8px 18px; font-size: 13px; font-weight: 700;
}
QPushButton#menu:hover { background: rgba(255, 255, 255, 52); }
QPushButton#menu:pressed { background: rgba(86, 182, 255, 90); }
#appheader { background: transparent; }
#title { font-size: 16px; font-weight: 700; letter-spacing: 0.4px; }
#subtitle { color: rgba(232, 240, 255, 205); font-size: 11px; }
#langTag { color: rgba(190, 222, 255, 215); font-size: 10px; letter-spacing: 0.5px; }
#footerText { color: rgba(206, 220, 248, 190); font-size: 11px; }
QComboBox {
    background: rgba(255, 255, 255, 28); border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 11px; padding: 7px 12px; color: #eef3ff;
}
QComboBox:hover { background: rgba(255, 255, 255, 48); }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #1b2130; border: none; selection-background-color: rgba(120, 180, 255, 120);
    color: #eef3ff; outline: none;
}
/* ---- drop zone: the one strongly tinted surface, so the target is unmistakable ---- */
#drop {
    background: rgba(255, 110, 160, 30);
    border: 1px solid rgba(255, 170, 205, 55);
    border-radius: 18px;
}
#drop[hot="true"] { background: rgba(255, 150, 190, 68); }
#dropTitle { font-size: 15px; font-weight: 600; color: #f5f9ff; }
#dropHint { color: rgba(228, 238, 255, 200); font-size: 12px; }
#sectionTitle { color: rgba(212, 224, 252, 225); font-size: 11px; font-weight: 600; letter-spacing: 1px; }
QPushButton#ghost {
    background: rgba(255, 255, 255, 28); border: 1px solid rgba(255, 255, 255, 38);
    border-radius: 11px; padding: 8px 16px;
}
QPushButton#ghost:hover { background: rgba(255, 255, 255, 60); }
QPushButton#ghost:disabled { color: rgba(235, 240, 255, 120); background: rgba(255, 255, 255, 16); }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(86, 182, 255, 240), stop:1 rgba(158, 118, 255, 240));
    border: 1px solid rgba(255, 255, 255, 55); border-radius: 14px;
    padding: 12px 26px; font-size: 14px; font-weight: 700; color: #0b1020;
}
QPushButton#primary:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(120, 200, 255, 252), stop:1 rgba(180, 145, 255, 252)); }
QPushButton#primary:disabled { background: rgba(255, 255, 255, 30); color: rgba(235, 240, 255, 140); }
QPushButton#danger {
    background: rgba(255, 92, 104, 200); border: 1px solid rgba(255, 150, 160, 55);
    border-radius: 14px; padding: 12px 20px; font-weight: 700; color: white;
}
QPushButton#danger:hover { background: rgba(255, 116, 126, 232); }
QPushButton#danger:disabled { background: rgba(255, 255, 255, 26); color: rgba(235, 240, 255, 110); }
QLineEdit {
    background: rgba(255, 255, 255, 26); border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 11px; padding: 8px 12px; selection-background-color: rgba(120, 180, 255, 150);
}
QLineEdit:focus { background: rgba(255, 255, 255, 44); border: 1px solid rgba(160, 210, 255, 90); }
QLineEdit:disabled { color: rgba(220, 228, 250, 120); background: rgba(255, 255, 255, 18); }
QCheckBox { spacing: 8px; color: rgba(236, 242, 255, 245); }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 5px;
                       border: 1px solid rgba(255, 255, 255, 40); background: rgba(255, 255, 255, 40); }
QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 rgba(96, 190, 255, 250), stop:1 rgba(160, 120, 255, 250));
    border: 1px solid rgba(255, 255, 255, 60);
}
QProgressBar {
    background: rgba(255, 255, 255, 26); border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 7px; height: 14px; text-align: center;
    color: rgba(245, 248, 255, 245); font-size: 10px;
}
QProgressBar::chunk {
    border-radius: 7px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(86, 182, 255, 245), stop:1 rgba(158, 118, 255, 245));
}
#log {
    background: transparent; border: none; padding: 9px;
    color: rgba(232, 240, 255, 245);
    font-family: "Microsoft YaHei", "Microsoft YaHei UI", sans-serif;
    font-size: 11px; font-weight: 700;
}
#stat { color: rgba(232, 240, 255, 240); font-size: 12px; }
#statAccent { color: #a8dcff; font-weight: 600; }
QScrollArea { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 80); border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
/* Dialogs follow the theme too: without these the message boxes keep the OS palette and
   show dark-on-dark when the app runs light. */
QDialog { background: #1b2130; }
QDialog QLabel { color: #eef3ff; background: transparent; }
QDialog QPushButton, QMessageBox QPushButton {
    background: rgba(255, 255, 255, 28); border: 1px solid rgba(255, 255, 255, 38);
    border-radius: 9px; padding: 6px 18px; min-width: 70px;
}
QDialog QPushButton:hover, QMessageBox QPushButton:hover { background: rgba(255, 255, 255, 60); }
"""

# Same structure as STYLE, with the surfaces inverted. Kept as a separate sheet rather than
# computed from STYLE: a colour-substitution pass would silently miss any rule added later.
LIGHT_STYLE = """
QWidget { color: #16202e; font-family: "Microsoft YaHei", "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; font-weight: 700; }
#root { background: transparent; }
#card { background: rgba(255, 255, 255, 36); border: none; border-radius: 22px; }
#card[solid="true"] { background: rgba(250, 251, 255, 246); }
#panel { background: rgba(255, 255, 255, 14); border: 1px solid rgba(20, 40, 80, 34); border-radius: 16px; }
#panelOut { background: rgba(86, 160, 255, 34); border: 1px solid rgba(86, 160, 255, 86); border-radius: 16px; }
#panelOpt { background: rgba(46, 200, 150, 30); border: 1px solid rgba(46, 190, 150, 86); border-radius: 16px; }
#panelRun { background: rgba(158, 118, 255, 34); border: 1px solid rgba(158, 118, 255, 86); border-radius: 16px; }
QPushButton#menu {
    background: rgba(255, 255, 255, 70);
    border: 1px solid rgba(20, 40, 80, 34);
    border-radius: 9px; padding: 8px 18px; font-size: 13px; font-weight: 700;
}
QPushButton#menu:hover { background: rgba(255, 255, 255, 160); }
QPushButton#menu:pressed { background: rgba(86, 160, 255, 110); }
#appheader { background: transparent; }
#title { font-size: 16px; font-weight: 700; letter-spacing: 0.4px; color: #10203a; }
#subtitle { color: rgba(30, 45, 70, 190); font-size: 11px; }
#langTag { color: rgba(40, 70, 120, 200); font-size: 10px; letter-spacing: 0.5px; }
#footerText { color: rgba(40, 55, 80, 190); font-size: 11px; }
QComboBox {
    background: rgba(255, 255, 255, 120); border: 1px solid rgba(20, 40, 80, 52);
    border-radius: 11px; padding: 7px 12px; color: #16202e;
}
QComboBox:hover { background: rgba(255, 255, 255, 170); }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #ffffff; border: none; selection-background-color: rgba(86, 160, 255, 70);
    color: #16202e; outline: none;
}
#drop { background: rgba(255, 110, 160, 44); border: 1px solid rgba(255, 120, 170, 96); border-radius: 18px; }
#drop[hot="true"] { background: rgba(255, 150, 190, 96); }
#dropTitle { font-size: 15px; font-weight: 600; color: #10203a; }
#dropHint { color: rgba(45, 60, 90, 195); font-size: 12px; }
#sectionTitle { color: rgba(40, 58, 90, 215); font-size: 11px; font-weight: 600; letter-spacing: 1px; }
QPushButton#ghost {
    background: rgba(255, 255, 255, 120); border: 1px solid rgba(20, 40, 80, 52);
    border-radius: 11px; padding: 8px 16px;
}
QPushButton#ghost:hover { background: rgba(255, 255, 255, 170); }
QPushButton#ghost:disabled { color: rgba(30, 45, 70, 110); background: rgba(255, 255, 255, 90); }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4a9eff, stop:1 #8a6cff);
    border: 1px solid rgba(255, 255, 255, 160); border-radius: 14px;
    padding: 12px 26px; font-size: 14px; font-weight: 700; color: #ffffff;
}
QPushButton#primary:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #62abff, stop:1 #9c80ff); }
QPushButton#primary:disabled { background: rgba(30, 45, 70, 46); color: rgba(30, 45, 70, 120); }
QPushButton#danger {
    background: rgba(240, 84, 96, 235); border: 1px solid rgba(255, 255, 255, 120);
    border-radius: 14px; padding: 12px 20px; font-weight: 700; color: white;
}
QPushButton#danger:hover { background: rgba(250, 104, 114, 250); }
QPushButton#danger:disabled { background: rgba(30, 45, 70, 36); color: rgba(30, 45, 70, 110); }
QLineEdit {
    background: rgba(255, 255, 255, 120); border: 1px solid rgba(20, 40, 80, 52);
    border-radius: 11px; padding: 8px 12px; selection-background-color: rgba(86, 160, 255, 90);
}
QLineEdit:focus { background: #ffffff; border: 1px solid rgba(86, 160, 255, 170); }
QLineEdit:disabled { color: rgba(30, 45, 70, 110); background: rgba(255, 255, 255, 110); }
QCheckBox { spacing: 8px; color: rgba(25, 40, 65, 240); }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 5px;
                       border: 1px solid rgba(20, 40, 80, 62); background: rgba(255, 255, 255, 140); }
QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4a9eff, stop:1 #8a6cff);
    border: 1px solid rgba(86, 160, 255, 170);
}
QProgressBar {
    background: rgba(255, 255, 255, 110); border: 1px solid rgba(20, 40, 80, 40);
    border-radius: 7px; height: 14px; text-align: center;
    color: rgba(20, 35, 60, 235); font-size: 10px;
}
QProgressBar::chunk {
    border-radius: 7px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4a9eff, stop:1 #8a6cff);
}
#log {
    background: transparent; border: none; padding: 9px;
    color: rgba(20, 35, 60, 240);
    font-family: "Microsoft YaHei", "Microsoft YaHei UI", sans-serif;
    font-size: 11px; font-weight: 700;
}
#stat { color: rgba(25, 40, 65, 240); font-size: 12px; }
#statAccent { color: #2a6fd6; font-weight: 600; }
QScrollArea { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(30, 50, 80, 110); border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QDialog { background: #ffffff; }
QDialog QLabel { color: #16202e; background: transparent; }
QDialog QPushButton, QMessageBox QPushButton {
    background: rgba(255, 255, 255, 235); border: 1px solid rgba(20, 40, 80, 60);
    border-radius: 9px; padding: 6px 18px; min-width: 70px; color: #16202e;
}
QDialog QPushButton:hover, QMessageBox QPushButton:hover { background: #ffffff; }
"""

# Interface strings. One table per language, keyed by the same names, so a missing key is
# visible as an odd label rather than a crash; ``MainWindow.t`` falls back to zh then to
# the key itself.
STRINGS = {
    "zh": {
        "app_name": "NCM 本地转换器",
        "app_sub": "NCM Converter",
        "menu_general": "通用",
        "menu_help": "帮助",
        "menu_about": "关于",
        "menu_view": "查看",
        "menu_version": "版本 %s",
        "check_update": "检查更新",
        "update_tooltip": "从 GitHub Releases 检查是否有新版本（需要联网）",
        "checking": "检查中…",
        "update_available": "有新版本",
        "update_latest": "已是最新版本",
        "update_failed": "检查失败，",
        "view_releases": "手动查看 Releases",
        "drop_title": "把 .ncm 文件或文件夹拖到这里",
        "drop_hint": "也可以点下面的按钮选择；支持递归目录",
        "out_dir": "输出目录",
        "out_dir_ph": "留空 = 转换到每个 .ncm 所在目录",
        "choose": "选择…",
        "organize": "整理方式",
        "organize_none": "不分类（全部放输出目录）",
        "organize_lang": "按语言分目录",
        "organize_artist": "按歌手分目录",
        "organize_both": "按语言 + 歌手（两级）",
        "preview": "预览落点",
        "preview_tip": "不转换，只列出每首歌会落到哪个文件夹",
        "dir_names": "目录名",
        "lang_zh": "中文",
        "lang_ja": "日文（含假名）",
        "lang_ko": "韩文",
        "lang_ru": "俄文（西里尔）",
        "lang_other": "其他（英法等拉丁字母）",
        "folder_zh": "中文",
        "folder_ja": "日文",
        "folder_ko": "韩文",
        "folder_ru": "俄文",
        "folder_other": "其他",
        "lang_tip": "检测为「%s」的歌曲放进哪个文件夹（可改）",
        "keep": "保留 .ncm",
        "lyrics": "嵌入歌词",
        "verify": "删除前逐字节校验",
        "cover": "下载封面",
        "go": "开始转换",
        "stop": "停止",
        "open_out": "打开输出目录",
        "waiting": "等待任务",
        "ready": "就绪",
        "version_tip": "版本 %s，由 %s 开发",
        "win_min": "最小化",
        "win_max": "最大化 / 还原（贴合）",
        "win_close": "关闭",
        "settings_title": "通用",
        "language": "语言",
        "lang_system": "跟随系统",
        "theme": "主题",
        "theme_light": "日间",
        "theme_dark": "夜间",
        "theme_system": "跟随系统",
        "apply": "应用",
        "close": "关闭",
        "help_title": "帮助",
        "help_text": (
            "把 .ncm 文件或整个文件夹拖进窗口，选好输出目录与整理方式，点「开始转换」。\n\n"
            "· 输出目录留空时，每首歌会转换到它自己所在的目录。\n"
            "· 「预览落点」不转换，只列出每首歌会落到哪个文件夹。\n"
            "· 勾选「删除前逐字节校验」后，只有解出的音频与落盘文件完全一致才会删除源文件。\n"
            "· 「嵌入歌词」只控制是否额外导出 .lrc 旁文件，标签里的歌词始终会写入。\n"
            "· 拖动窗口到屏幕边缘可吸附；双击标题栏可最大化。\n\n"
            "遇到问题时请到仓库 Issues 反馈。"
        ),
        "about_title": "关于",
        "about_text": (
            "NCM 本地转换器 %s\n\n"
            "把网易云音乐的 .ncm 文件在本地转换成可播放的 FLAC/MP3。\n"
            "全程离线：不联网、不登录、不上传任何文件。\n\n"
            "开发者：%s\n"
            "许可证：MIT\n"
            "项目主页：%s"
        ),
        "about_ok": "好",
        "lang_changed": "界面语言已切换，需要重启后完全生效。",
        "drop_selected": "已选择 %d 个 .ncm（%s）",
        "drop_more": "可以继续拖入更多文件；点「开始转换」即可",
        "pending": "待转换 %d 个文件 · %s",
        "add_none": "请先拖入或选择 .ncm 文件",
        "add_new": "已加入 %d 个文件",
        "add_dup": "没有找到新的 .ncm 文件",
        "other_empty": "「其他」目录名不能为空（其他语种会没有去处）",
        "other_empty_short": "「其他」目录名不能为空",
        "out_unusable": "输出目录不可用：%s",
        "out_missing": "输出目录不存在：%s（未指定时输出在源文件所在目录）",
        "empty_paren": "(空)",
        "open_failed": "打开失败：%s",
        "cleaned": "清理了 %d 个上次中断残留的临时文件",
        "started": "开始转换 %d 个文件（%s）→ %s",
        "src_dir": "源文件所在目录",
        "stopping": "正在停止…（已开始的当前文件会跑完）",
        "fatal": "致命错误：",
        "stage_metadata": "读取信息",
        "stage_decrypt": "解密音频",
        "stage_cover": "下载封面",
        "stage_tag": "写入标签",
        "stage_verify": "校验",
        "processing": "正在处理：%s  ·  %s",
        "item_failed": "✗ %s  失败：%s",
        "progress": "完成 %d/%d · 成功 %d · 失败 %d",
        "done_summary": "完成：成功 %d，失败 %d，用时 %.1fs",
        "done_stat": "完成 %d/%d · 成功 %d · 失败 %d · 用时 %.1fs",
        "preview_title": "落点预览（共 %d 首）：",
        "preview_row": "    %-34s %d 首",
        "src_dir_label": "（源目录）",
        "read_failed": "（读取失败：%s）",
        "unknown_reason": "未知原因",
        "update_found": "发现新版本 %s：%s",
        "update_latest_log": "已是最新版本（v%s）",
        "update_unavailable": "更新检查不可用：%s",
        "log_checking": "正在检查更新…（%s）",
        "choose_out_dir": "选择输出目录",
        "out_dir_set": "输出目录：",
    },
    "en": {
        "app_name": "NCM Converter",
        "app_sub": "NCM Converter",
        "menu_general": "General",
        "menu_help": "Help",
        "menu_about": "About",
        "menu_view": "View",
        "menu_version": "Version %s",
        "check_update": "Check for updates",
        "update_tooltip": "Ask GitHub Releases whether a newer version exists (needs network)",
        "checking": "Checking…",
        "update_available": "Update available",
        "update_latest": "You have the latest version",
        "update_failed": "Check failed, ",
        "view_releases": "open Releases manually",
        "drop_title": "Drop .ncm files or folders here",
        "drop_hint": "or use the buttons below; subfolders are searched too",
        "out_dir": "Output folder",
        "out_dir_ph": "empty = convert next to each .ncm",
        "choose": "Browse…",
        "organize": "Organise",
        "organize_none": "Flat (everything in the output folder)",
        "organize_lang": "By language",
        "organize_artist": "By artist",
        "organize_both": "Language then artist (two levels)",
        "preview": "Preview",
        "preview_tip": "Do not convert; just list where each track would land",
        "dir_names": "Folder names",
        "lang_zh": "Chinese",
        "lang_ja": "Japanese (with kana)",
        "lang_ko": "Korean",
        "lang_ru": "Russian (Cyrillic)",
        "lang_other": "Other (Latin script)",
        "folder_zh": "Chinese",
        "folder_ja": "Japanese",
        "folder_ko": "Korean",
        "folder_ru": "Russian",
        "folder_other": "Other",
        "lang_tip": "Folder for tracks detected as %s (editable)",
        "keep": "Keep .ncm",
        "lyrics": "Embed lyrics",
        "verify": "Byte-for-byte check",
        "cover": "Download cover",
        "go": "Convert",
        "stop": "Stop",
        "open_out": "Open output",
        "waiting": "Idle",
        "ready": "Ready",
        "version_tip": "Version %s by %s",
        "win_min": "Minimise",
        "win_max": "Maximise / restore (snap)",
        "win_close": "Close",
        "settings_title": "General",
        "language": "Language",
        "lang_system": "Follow system",
        "theme": "Theme",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "theme_system": "Follow system",
        "apply": "Apply",
        "close": "Close",
        "help_title": "Help",
        "help_text": (
            "Drag .ncm files or a whole folder into the window, pick an output folder and how\n"
            "to organise it, then press Convert.\n\n"
            "· With an empty output folder, each track is written next to its own .ncm.\n"
            "· Preview lists where every track would land without converting anything.\n"
            "· With the byte-for-byte check on, a source file is only deleted after the\n"
            "  written audio matches what was decrypted.\n"
            "· Embed lyrics only controls the extra .lrc sidecar; the tags always carry them.\n"
            "· Drag the window to a screen edge to snap; double-click the title strip to\n"
            "  maximise.\n\n"
            "Please report problems in the repository Issues."
        ),
        "about_title": "About",
        "about_text": (
            "NCM Converter %s\n\n"
            "Converts NetEase Cloud Music .ncm files to playable FLAC/MP3, locally.\n"
            "Fully offline: no network, no account, nothing uploaded.\n\n"
            "Developer: %s\n"
            "Licence: MIT\n"
            "Project: %s"
        ),
        "about_ok": "OK",
        "lang_changed": "Interface language saved; restart to apply it fully.",
        "drop_selected": "%d .ncm selected (%s)",
        "drop_more": "Drop more files, or press Convert",
        "pending": "%d file(s) queued · %s",
        "add_none": "Drop or choose .ncm files first",
        "add_new": "Added %d file(s)",
        "add_dup": "No new .ncm files found",
        "other_empty": "The 'other' folder name cannot be empty (those tracks would have nowhere to go)",
        "other_empty_short": "The 'other' folder name cannot be empty",
        "out_unusable": "Output folder unusable: %s",
        "out_missing": "Output folder missing: %s (with none set, files land beside their source)",
        "empty_paren": "(empty)",
        "open_failed": "Could not open: %s",
        "cleaned": "Removed %d temp file(s) left by an interrupted run",
        "started": "Converting %d file(s) (%s) -> %s",
        "src_dir": "source folders",
        "stopping": "Stopping… (the current file will finish)",
        "fatal": "Fatal error: ",
        "stage_metadata": "reading metadata",
        "stage_decrypt": "decrypting audio",
        "stage_cover": "fetching cover",
        "stage_tag": "writing tags",
        "stage_verify": "verifying",
        "processing": "Working: %s  ·  %s",
        "item_failed": "✗ %s  failed: %s",
        "progress": "%d/%d done · %d ok · %d failed",
        "done_summary": "Done: %d ok, %d failed, %.1fs",
        "done_stat": "%d/%d done · %d ok · %d failed · %.1fs",
        "preview_title": "Preview: %d track(s)",
        "preview_row": "    %-34s %d",
        "src_dir_label": "(source folder)",
        "read_failed": "(read failed: %s)",
        "unknown_reason": "unknown reason",
        "update_found": "New version %s: %s",
        "update_latest_log": "Already up to date (v%s)",
        "update_unavailable": "Update check unavailable: %s",
        "log_checking": "Checking for updates… (%s)",
        "choose_out_dir": "Choose output folder",
        "out_dir_set": "Output folder: ",
    },
}

LANG_CHOICES = ("system", "zh", "en")
THEME_CHOICES = ("system", "light", "dark")


def _settings_path() -> str:
    """Where the language/theme choice is remembered between runs."""
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    return os.path.join(base or os.path.expanduser("~"), "settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:                                  # noqa: BLE001 - missing/broken file is fine
        return {}


def save_settings(data: dict) -> None:
    try:
        path = _settings_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:                                  # noqa: BLE001 - never block the UI
        pass


def detect_language() -> str:
    """zh when the system UI language is Chinese, otherwise en.

    GetUserDefaultUILanguage is asked first: QLocale.system() follows the *process* locale,
    which a launcher or parent shell can leave as C, so the Qt answer alone is unreliable.
    """
    try:
        import ctypes

        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "zh" if (lang_id & 0x3FF) == 0x04 else "en"
    except Exception:                                  # noqa: BLE001 - never fatal
        pass
    try:
        return "zh" if QLocale.system().language() == QLocale.Language.Chinese else "en"
    except Exception:                                  # noqa: BLE001 - never fatal
        return "en"


def detect_system_dark() -> bool:
    """Windows' "app mode" choice; dark when unreadable (matches the glass look)."""
    try:
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return not bool(light)
    except Exception:                                  # noqa: BLE001
        return True


def resolve_language(setting: str) -> str:
    return detect_language() if setting == "system" else setting


def resolve_dark(setting: str) -> bool:
    return detect_system_dark() if setting == "system" else setting == "dark"



class DropZone(QFrame):
    """Drag-and-drop target for .ncm files and folders."""

    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setProperty("hot", "false")
        self.setMinimumHeight(132)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        self.icon = QLabel("🎵")
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setStyleSheet("font-size: 30px;")
        self.title = QLabel("")
        self.title.setObjectName("dropTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.hint = QLabel("")
        self.hint.setObjectName("dropHint")
        self.hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.icon)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)

    def _set_hot(self, hot: bool):
        self.setProperty("hot", "true" if hot else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_hot(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_hot(False)

    def dropEvent(self, event):
        self._set_hot(False)
        paths = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()

    def mousePressEvent(self, event):
        # the OS title bar handles window movement now; nothing to do here
        event.ignore()


class AppHeader(QFrame):
    """Draggable title strip carrying the app name and the coloured window dots.

    The window is frameless now, so this strip owns window dragging and
    double-click-to-maximise: the three dots on the right are the only window buttons
    (green = minimise, yellow = maximise / restore, red = close).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("appheader")
        self.setFixedHeight(44)

    # The OS title bar is gone, so window dragging and double-click maximise live here.
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            win = self.window()
            if win.isMaximized():
                win.showNormal()
            else:
                win.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class GlassBackdrop(QWidget):
    """Hosts the window contents and lets the native blurred backdrop show through.

    The window itself carries no background now: ``pywinstyles.apply_style(hwnd,
    "acrylic")`` makes Qt paint the client area transparent and extends the DWM frame over
    the whole window, so Windows composites its blurred material edge to edge. This widget
    therefore only paints the rounded shape and the four corner cut-outs; everything else
    is the OS material seen through the window.
    """

    RADIUS = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled = True
        self._tint = QColor(10, 14, 24, 96)

    def set_enabled(self, on: bool):
        self._enabled = on
        self.update()

    def set_tint(self, color: QColor):
        self._tint = color
        self.update()

    # kept for API compatibility (nothing to capture or refresh any more)
    def schedule(self):
        pass

    def update_backdrop(self):
        pass

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, self.RADIUS, self.RADIUS)
        painter.setClipPath(path)
        if not self._enabled:
            painter.fillRect(self.rect(), QColor(20, 24, 36))
        else:
            # a light tint only: the blur comes from the OS, and a heavy fill would hide it
            painter.fillRect(self.rect(), self._tint)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule()

    def showEvent(self, event):
        super().showEvent(event)
        # let the window appear first, otherwise we would capture ourselves
        QTimer.singleShot(120, self.update_backdrop)


class SettingsDialog(QDialog):
    """通用 dialog: interface language and theme.

    Theme applies immediately; language needs a restart because every string is baked into
    its widget at construction time.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(parent.t("settings_title"))
        self.setModal(True)
        self.setMinimumWidth(380)

        self.cmb_lang = QComboBox()
        self.cmb_lang.addItem(parent.t("lang_system"), "system")
        self.cmb_lang.addItem("中文", "zh")
        self.cmb_lang.addItem("English", "en")
        self.cmb_lang.setCurrentIndex(max(0, self.cmb_lang.findData(parent.lang_setting)))

        self.cmb_theme = QComboBox()
        self.cmb_theme.addItem(parent.t("theme_system"), "system")
        self.cmb_theme.addItem(parent.t("theme_light"), "light")
        self.cmb_theme.addItem(parent.t("theme_dark"), "dark")
        self.cmb_theme.setCurrentIndex(max(0, self.cmb_theme.findData(parent.theme_setting)))

        form = QFormLayout()
        form.addRow(parent.t("language"), self.cmb_lang)
        form.addRow(parent.t("theme"), self.cmb_theme)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Ok).setText(parent.t("apply"))
        buttons.button(QDialogButtonBox.Cancel).setText(parent.t("close"))

        box = QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(buttons)

    def result_settings(self):
        return self.cmb_lang.currentData(), self.cmb_theme.currentData()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        from ncmdump.ncm2mp3 import find_inputs, human, sanitize
        from ncmdump.tags import LANGUAGES
        self._sanitize = sanitize
        self._human = human
        self._find_inputs = find_inputs
        self._languages = LANGUAGES

        # Frameless glass window. The OS title bar is replaced by our own 40 px strip so
        # the coloured window dots can sit exactly where the system buttons used to be.
        # Trade-off (documented in docs/GLASS-TROUBLESHOOTING.md): a frameless window loses
        # the shell's Aero Snap and edge-resize hit areas, so drag / double-click maximise /
        # edge resize are re-implemented below on top of QWindow.startSystemMove and
        # startSystemResize.
        # Interface language and theme: an explicit NCM_LANG / NCM_THEME wins (so a headless
        # screenshot run is deterministic), then the choice saved by the settings dialog,
        # then follow the OS.
        saved = load_settings()
        lang_raw = os.environ.get("NCM_LANG") or saved.get("language") or "system"
        self.lang_setting = str(lang_raw).strip().lower()
        if self.lang_setting not in LANG_CHOICES:
            self.lang_setting = "system"
        self.lang = resolve_language(self.lang_setting)
        theme_raw = os.environ.get("NCM_THEME") or saved.get("theme") or "system"
        self.theme_setting = str(theme_raw).strip().lower()
        if self.theme_setting not in THEME_CHOICES:
            self.theme_setting = "system"

        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("root")
        self.setWindowTitle(self.t("app_name"))
        self.setWindowIcon(make_icon())
        self.resize(980, 820)
        # The content genuinely needs this much: header + drop zone + three stacked panels
        # + log. Shrinking below it used to squeeze every row to its minimum and clip text,
        # so the floor is raised to the real requirement instead.
        self.setMinimumSize(900, 760)
        self._resize_edges = Qt.Edges()

        # Workers never touch widgets: they post plain dicts here and the main thread's
        # timer drains it. (An earlier signal-based bridge was wired up but never emitted,
        # which just looked like a safety mechanism without being one.)
        self.queue = Queue()
        self.cancel_flag = threading.Event()
        self.pool = None
        self.files = []
        self.total = 0
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.bytes_done = 0
        self.started_at = 0.0
        self._glass_on = True
        # The window material is fixed to Acrylic at build time: no switching UI, no dead
        # code paths. NCM_GLASS=<acrylic|mica|aero|solid> remains as a debugging escape
        # hatch so a build can be tested against a different material without editing code.
        override = os.environ.get("NCM_GLASS", "").strip().lower()
        self._material = override if override in WINDOW_MATERIALS else DEFAULT_MATERIAL
        self._glass_on = self._material != "solid"

        self._apply_theme()
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._drain_queue)
        # Always ticking. It used to be started by start() and stopped on "done", which was
        # fine while conversion was the only producer on the queue - but the update check
        # also posts its result there, so with no conversion running the "检查中…" label
        # never got replaced and the button looked broken.
        self.timer.start()

    # -------------------------------------------------------------- language / theme
    def t(self, key: str) -> str:
        """Interface string for ``key`` in the active language, falling back to Chinese."""
        table = STRINGS.get(self.lang) or STRINGS["zh"]
        return table.get(key) or STRINGS["zh"].get(key) or key

    def _style_sheet(self) -> str:
        """The theme sheet for the current 日间/夜间/跟随系统 choice."""
        return STYLE if resolve_dark(self.theme_setting) else LIGHT_STYLE

    def _apply_theme(self):
        """Re-apply both the app and the window sheet for the current theme choice."""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(self._style_sheet())
        self.setStyleSheet(window_stylesheet(self._glass_on, self._style_sheet()))

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        # The glass fills the WHOLE window; the card is just an inner panel on top of it.
        # Earlier the card had a margin and nothing painted behind it, which showed up as
        # an unexplained transparent ring around the app.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.backdrop = GlassBackdrop(self)
        root.addWidget(self.backdrop)

        overlay = QVBoxLayout(self.backdrop)
        # No outer gutter: the user wants the app to look like a native window, and a
        # transparent ring around the card reads as "an extra layer" that should not be
        # there. The card now runs edge to edge.
        overlay.setContentsMargins(0, 0, 0, 0)
        overlay.setSpacing(0)

        card = QFrame()
        card.setObjectName("card")
        self.card = card
        overlay.addWidget(card)

        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(12)

        # ---- title strip: app name on the left, coloured window dots on the right
        self.header = AppHeader(card)
        bar = QHBoxLayout(self.header)
        bar.setContentsMargins(2, 0, 2, 0)
        bar.setSpacing(8)
        dot = QLabel("◍")
        dot.setStyleSheet("color: #7fc4ff; font-size: 16px;")
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        self.lbl_title = QLabel(self.t("app_name"))
        self.lbl_title.setObjectName("title")
        self.lbl_sub = QLabel(self.t("app_sub"))
        self.lbl_sub.setObjectName("subtitle")
        # Let the whole strip drag: a label that swallows the press would otherwise punch a
        # dead hole in the title bar.
        for label in (dot, self.lbl_title, self.lbl_sub):
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        title_box.addWidget(self.lbl_title)
        title_box.addWidget(self.lbl_sub)
        bar.addWidget(dot)
        bar.addLayout(title_box)
        bar.addStretch(1)

        # ---- menu: general / help / about live in the title strip, so there is no
        # separate menu bar eating vertical space.
        self.btn_general = QPushButton(self.t("menu_general"))
        self.btn_help = QPushButton(self.t("menu_help"))
        self.btn_about = QPushButton(self.t("menu_about"))
        for btn in (self.btn_general, self.btn_help, self.btn_about):
            btn.setObjectName("menu")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(32)
            bar.addWidget(btn)
        self.btn_general.clicked.connect(self.open_settings)
        self.btn_help.clicked.connect(self.open_help)
        self.btn_about.clicked.connect(self.open_about)
        bar.addSpacing(12)

        # ---- update check: moved up from the footer, next to the menus
        self.lbl_update = QLabel("")
        self.lbl_update.setObjectName("footerText")
        self.lbl_update.setOpenExternalLinks(True)
        self.btn_update = QPushButton(self.t("check_update"))
        self.btn_update.setObjectName("ghost")
        self.btn_update.setToolTip(self.t("update_tooltip"))
        self.btn_update.setFixedHeight(32)
        self.btn_update.clicked.connect(self.check_updates)
        bar.addWidget(self.lbl_update)
        bar.addWidget(self.btn_update)
        bar.addSpacing(10)

        # ---- coloured window dots: they replace the OS title-bar buttons.
        # Order matches the request: minimise, maximise/restore ("贴合"), close.
        self.btn_min = self._window_dot("#7bed9f", "−", self.t("win_min"))
        self.btn_max = self._window_dot("#ffd166", "＋", self.t("win_max"))
        self.btn_close = self._window_dot("#ff6b81", "×", self.t("win_close"))
        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_max.clicked.connect(self._toggle_maximized)
        self.btn_close.clicked.connect(self.close)
        dots = QHBoxLayout()
        dots.setSpacing(8)
        dots.addWidget(self.btn_min)
        dots.addWidget(self.btn_max)
        dots.addWidget(self.btn_close)
        bar.addLayout(dots)
        outer.addWidget(self.header)

        # ---- drop zone
        self.drop = DropZone()
        self.drop.filesDropped.connect(self.add_paths)
        self.drop.title.setText(self.t("drop_title"))
        self.drop.hint.setText(self.t("drop_hint"))
        outer.addWidget(self.drop)

        # ---- functional group 1: where the files go (tinted glass)
        panel_out = self._panel("panelOut")
        p_out = QVBoxLayout(panel_out)
        p_out.setContentsMargins(14, 12, 14, 14)
        p_out.setSpacing(10)

        # output row
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(self.t("out_dir"))
        lbl.setObjectName("sectionTitle")
        self.ed_out = QLineEdit(os.environ.get("NCM_OUT_DIR", ""))
        self.ed_out.setPlaceholderText(self.t("out_dir_ph"))
        self.btn_out = QPushButton(self.t("choose"))
        self.btn_out.setObjectName("ghost")
        self.btn_out.clicked.connect(self.pick_output)
        row.addWidget(lbl)
        row.addWidget(self.ed_out, 1)
        row.addWidget(self.btn_out)
        # (added to the bottom strip further down, together with the run controls)

        # ---- organize: mode + per-language folder names (all editable)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_label = QLabel(self.t("organize"))
        mode_label.setObjectName("sectionTitle")
        self.cmb_organize = QComboBox()
        self.cmb_organize.addItems([
            self.t("organize_none"),
            self.t("organize_lang"),
            self.t("organize_artist"),
            self.t("organize_both"),
        ])
        self.cmb_organize.setCurrentIndex(1)
        self.cmb_organize.currentIndexChanged.connect(self._on_organize_changed)
        self.btn_plan = QPushButton(self.t("preview"))
        self.btn_plan.setObjectName("ghost")
        self.btn_plan.setToolTip(self.t("preview_tip"))
        self.btn_plan.clicked.connect(self.preview_layout)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.cmb_organize, 1)
        mode_row.addWidget(self.btn_plan)
        p_out.addLayout(mode_row)

        # one editable field per language; the label shows the detected code
        self.lang_fields = {}
        lang_row = QHBoxLayout()
        lang_row.setSpacing(6)
        self.lbl_lang_hint = QLabel(self.t("dir_names"))
        self.lbl_lang_hint.setObjectName("sectionTitle")
        lang_row.addWidget(self.lbl_lang_hint)
        defaults = {code: self.t("folder_" + code) for code in self._languages}
        titles = {"zh": self.t("lang_zh"), "ja": self.t("lang_ja"),
                  "ko": self.t("lang_ko"), "ru": self.t("lang_ru"),
                  "other": self.t("lang_other")}
        for code in self._languages:
            box = QVBoxLayout()
            box.setSpacing(2)
            tag = QLabel(code)
            tag.setObjectName("langTag")
            edit = QLineEdit(defaults.get(code, code))
            edit.setToolTip(self.t("lang_tip") % titles.get(code, code))
            edit.setMinimumWidth(90)
            box.addWidget(tag)
            box.addWidget(edit)
            self.lang_fields[code] = edit
            lang_row.addLayout(box, 1)
        p_out.addLayout(lang_row)
        outer.addWidget(panel_out)

        # ---- functional group 2: conversion options (tinted glass)
        panel_opt = self._panel("panelOpt")
        p_opt = QVBoxLayout(panel_opt)
        p_opt.setContentsMargins(14, 10, 14, 12)
        p_opt.setSpacing(8)
        opts = QHBoxLayout()
        opts.setSpacing(16)
        self.chk_keep = QCheckBox(self.t("keep"))
        self.chk_lyrics = QCheckBox(self.t("lyrics"))
        # Off by default: the tag always carries the lyrics anyway, and this box only
        # controls the extra .lrc sidecar. Leaving it on made every conversion drop an
        # .lrc next to the audio, which reads as a surprise rather than a feature.
        self.chk_lyrics.setChecked(False)
        self.chk_verify = QCheckBox(self.t("verify"))
        self.chk_verify.setChecked(True)
        self.chk_cover = QCheckBox(self.t("cover"))
        for widget in (self.chk_keep, self.chk_lyrics, self.chk_verify, self.chk_cover):
            opts.addWidget(widget)
        opts.addStretch(1)
        p_opt.addLayout(opts)
        outer.addWidget(panel_opt)

        # ---- functional group 3: run controls (tinted glass)
        panel_run = self._panel("panelRun")
        p_run = QVBoxLayout(panel_run)
        p_run.setContentsMargins(14, 12, 14, 14)
        p_run.setSpacing(10)
        action = QHBoxLayout()
        action.setSpacing(10)
        self.btn_go = QPushButton(self.t("go"))
        self.btn_go.setObjectName("primary")
        self.btn_go.clicked.connect(self.start)
        self.btn_cancel = QPushButton(self.t("stop"))
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_open = QPushButton(self.t("open_out"))
        self.btn_open.setObjectName("ghost")
        self.btn_open.clicked.connect(self.open_output)
        action.addWidget(self.btn_go, 2)
        action.addWidget(self.btn_cancel, 1)
        action.addWidget(self.btn_open, 1)
        # (added to the bottom strip further down)

        # ---- progress
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        p_run.addWidget(self.bar)
        stats = QHBoxLayout()
        self.lbl_stat = QLabel(self.t("waiting"))
        self.lbl_stat.setObjectName("stat")
        self.lbl_speed = QLabel("")
        self.lbl_speed.setObjectName("statAccent")
        stats.addWidget(self.lbl_stat, 1)
        stats.addWidget(self.lbl_speed)
        p_run.addLayout(stats)
        outer.addWidget(panel_run)

        # ---- log
        self.log = QLabel("")
        self.log.setObjectName("log")
        self.log.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.log.setWordWrap(True)
        self.log.setTextInteractionFlags(Qt.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; } "
                            "QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setMinimumHeight(110)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(self.log)
        holder_layout.addStretch(1)
        scroll.setWidget(holder)
        self.log_scroll = scroll
        panel_log = self._panel("panel")
        p_log = QVBoxLayout(panel_log)
        p_log.setContentsMargins(6, 6, 6, 6)
        p_log.addWidget(scroll)
        outer.addWidget(panel_log, 1)

        # ---- bottom strip: output folder + run controls, moved out of the middle
        panel_bottom = self._panel("panelRun")
        p_bottom = QVBoxLayout(panel_bottom)
        p_bottom.setContentsMargins(14, 12, 14, 14)
        p_bottom.setSpacing(10)
        p_bottom.addLayout(row)
        p_bottom.addLayout(action)
        outer.addWidget(panel_bottom)

        # ---- footer: version only; the update check lives in the title strip now
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.lbl_version = QLabel("v%s · %s" % (VERSION, DEVELOPER))
        self.lbl_version.setObjectName("footerText")
        self.lbl_version.setToolTip(self.t("version_tip") % (VERSION, DEVELOPER))
        footer.addWidget(self.lbl_version)
        footer.addStretch(1)
        outer.addLayout(footer)

        # Widgets must not collapse below a usable height when the window is resized: QSS
        # padding alone does not raise the layout's minimum, it only pads the drawn box.
        # The two big buttons need more room than the inputs (12 px padding + 14 px text).
        for widget in (self.ed_out, self.cmb_organize, self.btn_out, self.btn_plan,
                       self.btn_open, self.btn_update, *self.lang_fields.values()):
            widget.setMinimumHeight(34)
        for widget in (self.btn_go, self.btn_cancel):
            widget.setMinimumHeight(42)

        self._on_organize_changed(self.cmb_organize.currentIndex())

    # ------------------------------------------------------------ menu dialogs
    def open_help(self):
        QMessageBox.information(self, self.t("help_title"), self.t("help_text"))

    def open_about(self):
        QMessageBox.information(
            self, self.t("about_title"),
            self.t("about_text") % (VERSION, DEVELOPER, REPO_URL))

    def open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        lang_setting, theme_setting = dlg.result_settings()
        old_lang = self.lang
        self.lang_setting = lang_setting
        self.theme_setting = theme_setting
        save_settings({"language": lang_setting, "theme": theme_setting})
        self._apply_theme()
        if resolve_language(lang_setting) != old_lang:
            self._restart()

    def _restart(self):
        """Rebuild the window in the newly chosen language.

        Every string is baked into its widget at construction time, so a language switch
        means building the interface again. Queued files, the output folder and the window
        position carry over; a folder name is kept only if the user actually renamed it.
        """
        files = list(self.files)
        out_dir = self.ed_out.text()
        position = self.pos()
        old_texts = {code: edit.text() for code, edit in self.lang_fields.items()}
        old_defaults = {code: STRINGS[self.lang].get("folder_" + code, "")
                        for code in old_texts}

        fresh = MainWindow()
        fresh.files = files
        fresh.ed_out.setText(out_dir)
        fresh.add_paths([])
        for code, edit in fresh.lang_fields.items():
            current = old_texts.get(code)
            if current and current != old_defaults.get(code):
                edit.setText(current)      # the user renamed it - do not throw that away
        fresh.move(position)
        fresh.show()
        self.close()

    # ----------------------------------------------------------- window chrome
    def _panel(self, name: str) -> QFrame:
        """A tinted glass panel that groups one functional block."""
        panel = QFrame()
        panel.setObjectName(name)
        panel.setAttribute(Qt.WA_StyledBackground, True)
        return panel

    def _window_dot(self, color: str, glyph: str, tip: str) -> QPushButton:
        """One coloured window dot; the glyph only shows on hover (macOS style)."""
        btn = QPushButton(glyph)
        btn.setObjectName("windot")
        btn.setToolTip(tip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setFixedSize(18, 18)
        btn.setStyleSheet(
            "QPushButton { background: %s; border: none; border-radius: 9px;"
            " color: transparent; font-size: 9px; font-weight: 700; padding: 0; }"
            "QPushButton:hover { color: rgba(18, 22, 30, 0.85); }" % color
        )
        return btn

    def _toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _enable_snap(self) -> None:
        """Put the window back in the shell's window list so Aero Snap works.

        ``FramelessWindowHint`` leaves the window as a bare ``WS_POPUP``. The shell then
        does not treat it as an application window at all: no Aero Snap, no drag-to-edge
        tiling, no ``Win`` + arrow. Re-adding the resize / maximise bits is what brings
        those back. ``WM_NCCALCSIZE`` in ``nativeEvent`` keeps the 7 px frame that the
        style implies out of the client area, so the look does not change.
        """
        if os.name != "nt":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            style = (style & ~WS_POPUP & ~WS_CAPTION
                     | WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX | WS_SYSMENU)
            user32.SetWindowLongW(hwnd, GWL_STYLE, ctypes.c_int32(style).value)
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
            _boot_log("snap enabled: style=%#010x" % (style & 0xFFFFFFFF))
        except Exception as exc:
            _boot_log("snap style failed: %s" % exc)

    #: Height of the in-app title strip; drag / Snap / maximise are delegated to the shell.
    TITLEBAR_HEIGHT = 44
    #: Right-hand zone holding the window dots, which must stay clickable (HTCLIENT).
    WINDOW_DOTS_WIDTH = 92

    def nativeEvent(self, eventType, message):
        """Hand hit-testing back to the shell — this is what makes a frameless window
        behave like a native one.

        Three things happen here:
          * edge / corner resize zones (a frameless window has no borders of its own),
          * HTCAPTION over the title strip, so Windows itself provides Aero Snap,
            drag-to-edge tiling, double-click-to-maximise and drag-to-top — the behaviour
            ``startSystemMove()`` alone does not give us, and
          * HTCLIENT over the coloured dots, so they stay clickable.
        """
        if sys.platform == "win32" and eventType == b"windows_generic_MSG":
            try:
                import ctypes
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_NCCALCSIZE:
                    # The frame style implies a 7 px border. Answering here (with the
                    # client rect left untouched) keeps the client area equal to the whole
                    # window, so the border never becomes visible.
                    return True, 0
                if msg.message == WM_NCHITTEST:
                    sx = ctypes.c_short(msg.lParam & 0xFFFF).value
                    sy = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                    rect = wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(
                        wintypes.HWND(int(self.winId())), ctypes.byref(rect))
                    cx = sx - rect.left
                    cy = sy - rect.top
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    border = 6
                    left = cx <= border
                    right = cx >= w - border
                    top = cy <= border
                    bottom = cy >= h - border
                    if left and top:
                        return True, 13                        # HTTOPLEFT
                    if right and top:
                        return True, 14                        # HTTOPRIGHT
                    if left and bottom:
                        return True, 16                        # HTBOTTOMLEFT
                    if right and bottom:
                        return True, 17                        # HTBOTTOMRIGHT
                    if left:
                        return True, 10                        # HTLEFT
                    if right:
                        return True, 11                        # HTRIGHT
                    if top:
                        return True, 12                        # HTTOP
                    if bottom:
                        return True, 15                        # HTBOTTOM
                    if cy < self.TITLEBAR_HEIGHT and cx < w - self.WINDOW_DOTS_WIDTH:
                        return True, 2                         # HTCAPTION
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_style_done", False):
            self._style_done = True
            try:
                self._apply_win_effects()
            except Exception as exc:      # a missing blur must never break the window
                _boot_log("window effects failed: %s" % exc)

    def _apply_win_effects(self):
        hwnd = int(self.winId())
        # Snap / tiling need the window to look like an app window to the shell, and that
        # same change is what gives us a normal drop shadow: with WS_THICKFRAME back, DWM
        # treats it as an ordinary window and draws the usual shadow. The old
        # DwmExtendFrameIntoClientArea(1,1,1,1) trick is therefore gone — it painted a 1 px
        # accent border around the whole window, which read as an unwanted extra ring.
        self._enable_snap()

        # Native window material through pywinstyles (acrylic by default), falling back to
        # the raw DWM backdrop API if the package is missing.
        applied = self._material
        if not self._glass_on:
            pywinstyles_normal(hwnd)
        else:
            applied = apply_window_style(self, self._material)
            if applied == "none":
                applied = apply_glass(hwnd, True)
        self._material = applied
        # pywinstyles' paint() replaces the widget stylesheet, so the real stylesheet has to
        # go on *after* the material call — otherwise the glass style gets wiped.
        self.setStyleSheet(window_stylesheet(self._glass_on, self._style_sheet()))
        # Colour the native frame to match the app, and keep the window border off.
        tune_native_frame(hwnd, glass_on=self._glass_on)
        self.backdrop.set_enabled(self._glass_on)
        # The OS does the blurring, so keep the tint very thin: a heavier fill would hide
        # exactly the effect we asked the system for. Raised transparency on request.
        self.backdrop.set_tint(QColor(8, 12, 22, GLASS_TINT_ALPHA if self._glass_on else 255))
        # The card style differs between glass and solid, so it has to be re-polished —
        # this is the only "UI" state the material has now that the badge is gone.
        self.card.setProperty("solid", "false" if self._glass_on else "true")
        self.card.style().unpolish(self.card)
        self.card.style().polish(self.card)
        _boot_log("window: material=%s hwnd=%d native_frame=True" % (self._material, hwnd))

    # ---------------------------------------------------------------- helpers
    def check_updates(self):
        """Ask GitHub for the latest release, off the UI thread."""
        if getattr(self, "_update_busy", False):
            return
        self._update_busy = True
        self.btn_update.setEnabled(False)
        self.lbl_update.setText(self.t("checking"))
        self.lbl_update.setOpenExternalLinks(False)
        self._append_log(self.t("log_checking") % RELEASES_URL)

        def work():
            try:
                from ncmdump.metadata import check_for_update
                info = check_for_update()
            except Exception as exc:                 # never let a button crash the app
                info = {"status": "unavailable", "error": "%s: %s" % (type(exc).__name__, exc)}
            self.queue.put(("update", info))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_result(self, info):
        self._update_busy = False
        self.btn_update.setEnabled(True)
        status = info.get("status")
        if status == "update":
            latest = info.get("latest", "")
            url = info.get("url") or RELEASES_URL
            self.lbl_update.setText(
                '<a href="%s" style="color:#9ad8ff;">%s %s</a>'
                % (url, self.t("update_available"), latest))
            self.lbl_update.setOpenExternalLinks(True)
            self._append_log(self.t("update_found") % (latest, url))
        elif status == "current":
            self.lbl_update.setText(self.t("update_latest"))
            self._append_log(self.t("update_latest_log") % VERSION)
        else:
            reason = info.get("error", self.t("unknown_reason"))
            self.lbl_update.setText(
                '%s<a href="%s" style="color:#9ad8ff;">%s</a>'
                % (self.t("update_failed"), RELEASES_URL, self.t("view_releases")))
            self.lbl_update.setOpenExternalLinks(True)
            self._append_log(self.t("update_unavailable") % reason)

    def organize_settings(self):
        """Translate the controls into (lang_map, organize, mode) for the converter.

        The language fields stay editable even in artist mode — they are simply unused —
        so switching modes never loses what the user typed.
        """
        index = self.cmb_organize.currentIndex()          # 0 none, 1 language, 2 artist, 3 both
        if index == 0:
            return None, False, "none"
        mapping = {"__mode__": "language" if index in (1, 3) else "artist",
                   "__artist__": index == 3}
        for code, field in self.lang_fields.items():
            name = field.text().strip()
            if name:
                mapping[code] = name
        if index in (1, 3) and not mapping.get("other"):
            return None, False, "invalid"                # "other" folder name is required
        return mapping, True, "ok"

    def _on_organize_changed(self, index: int):
        uses_language = index in (1, 3)
        for field in self.lang_fields.values():
            field.setEnabled(uses_language)
        self.lbl_lang_hint.setEnabled(uses_language)

    def preview_layout(self):
        """Show where each queued track would land, without converting anything."""
        if not self.files:
            self._append_log(self.t("add_none"))
            return
        mapping, organize, state = self.organize_settings()
        if state == "invalid":
            self._append_log(self.t("other_empty_short"))
            return
        from ncmdump.ncm2mp3 import _organize_options, _target_dir
        from ncmdump.ncm_core import decrypt_file

        mode, artist, folders = _organize_options(mapping if organize else None)
        out_dir = self.ed_out.text().strip()
        buckets = {}
        for path in self.files:
            try:
                parsed = decrypt_file(path, read_audio=False)
                target = _target_dir(path, out_dir, parsed, folders, organize, mode, artist)
            except Exception as exc:
                target = self.t("read_failed") % exc
            buckets.setdefault(target or self.t("src_dir_label"), 0)
            buckets[target or self.t("src_dir_label")] += 1
        base = os.path.abspath(out_dir) if out_dir else ""
        self._append_log(self.t("preview_title") % len(self.files))
        for target, count in sorted(buckets.items()):
            shown = target
            if base and os.path.abspath(target).startswith(base):
                shown = os.path.relpath(target, base)
            self._append_log(self.t("preview_row") % (shown, count))

    def pick_output(self):
        current = self.ed_out.text().strip()
        if not current:
            # no personal path baked in: start from the user's own Music folder
            current = QStandardPaths.writableLocation(QStandardPaths.MusicLocation) \
                or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, self.t("choose_out_dir"), current)
        if chosen:
            self.ed_out.setText(chosen)
            self._append_log(self.t("out_dir_set") + chosen)

    def open_output(self):
        path = self.ed_out.text().strip()
        if not path:
            path = os.path.dirname(os.path.abspath(self.files[0])) if self.files else ""
        if not path or not os.path.isdir(path):
            self._append_log(self.t("out_missing") % (path or self.t("empty_paren")))
            return
        try:
            os.startfile(path)  # noqa: S606  (Windows shell open)
        except Exception as exc:
            self._append_log(self.t("open_failed") % exc)

    def add_paths(self, paths):
        found = self._find_inputs(paths, recursive=True)
        known = {os.path.abspath(p).lower() for p in self.files}
        added = [p for p in found if os.path.abspath(p).lower() not in known]
        self.files.extend(added)
        total_bytes = sum(os.path.getsize(p) for p in self.files if os.path.isfile(p))
        self.drop.title.setText(
            self.t("drop_selected") % (len(self.files), self._human(total_bytes)))
        self.drop.hint.setText(self.t("drop_more"))
        self.lbl_stat.setText(
            self.t("pending") % (len(self.files), self._human(total_bytes)))
        if added:
            self._append_log(self.t("add_new") % len(added))
        else:
            self._append_log(self.t("add_dup"))

    def _append_log(self, text: str, tag: str = ""):
        prefix = time.strftime("%H:%M:%S ")
        line = prefix + text
        old = self.log.text()
        lines = [row for row in old.splitlines() if row.strip()]
        lines.append(line)
        if len(lines) > 400:
            lines = lines[-400:]
        self.log.setText("\n".join(lines))
        QTimer.singleShot(0, lambda: self.log_scroll.verticalScrollBar().setValue(
            self.log_scroll.verticalScrollBar().maximum()))

    # --------------------------------------------------------------- run
    def _clean_stale_tempfiles(self, out_dir: str) -> int:
        """Remove leftover `*.ncmtmp` files from a previously killed run.

        They are dot-prefixed and therefore invisible in Explorer, and a run that was
        force-killed cannot clean up after itself — so sweep them when a new run starts.
        Only files older than an hour are touched: a *live* worker's staging file has the
        same shape, and deleting that would corrupt an in-flight conversion.
        """
        removed = 0
        cutoff = time.time() - 3600
        roots = {os.path.dirname(os.path.abspath(p)) for p in self.files if os.path.isfile(p)}
        if out_dir:
            # the tree can now be two levels deep (language/artist), so walk the whole
            # output tree rather than guessing the sub-folder names
            roots.add(os.path.abspath(out_dir))
        seen = set()
        for root in list(roots):
            if not os.path.isdir(root):
                continue
            for base, _dirs, names in os.walk(root):
                if base in seen:
                    continue
                seen.add(base)
                for name in names:
                    if not (name.startswith(".") and name.endswith(".ncmtmp")):
                        continue
                    path = os.path.join(base, name)
                    try:
                        if os.path.getmtime(path) > cutoff:
                            continue          # possibly a running conversion
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
        return removed

    def start(self):
        if not self.files:
            self._append_log(self.t("add_none"))
            return
        out_dir = self.ed_out.text().strip()
        # an empty box means "next to each source file"; only create it when it is set,
        # and never fabricate a machine-specific default path
        if out_dir:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                self._append_log(self.t("out_unusable") % exc)
                return

        stale = self._clean_stale_tempfiles(out_dir)
        if stale:
            self._append_log(self.t("cleaned") % stale)

        lang_map, organize, state = self.organize_settings()
        if state == "invalid":
            self._append_log(self.t("other_empty"))
            return

        self.total = len(self.files)
        self.done = self.ok = self.failed = self.bytes_done = 0
        self.started_at = time.time()
        self.cancel_flag.clear()
        self.bar.setValue(0)
        self.btn_go.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.drop.setEnabled(False)
        self.log.setText("")
        self._append_log(self.t("started") % (
            self.total, self.cmb_organize.currentText(), out_dir or self.t("src_dir")))

        keep = self.chk_keep.isChecked()
        want_lyrics = self.chk_lyrics.isChecked()
        verify = self.chk_verify.isChecked()
        want_cover = self.chk_cover.isChecked()
        jobs = max(2, min(6, (os.cpu_count() or 4)))

        self.timer.start()
        thread = threading.Thread(
            target=self._worker,
            args=(list(self.files), out_dir, keep, want_cover, want_lyrics, verify,
                  lang_map, organize, jobs),
            daemon=True,
        )
        thread.start()

    def _worker(self, files, out_dir, keep, want_cover, want_lyrics, verify,
                lang_map, organize, jobs):
        from ncmdump.ncm2mp3 import _convert

        def run_one(src):
            if self.cancel_flag.is_set():
                return None
            name = os.path.basename(src)

            def progress(stage, fraction=0.0):
                self.queue.put(("stage", {"name": name, "stage": stage, "fraction": fraction}))

            return _convert(src, out_dir, keep, want_cover, want_lyrics, False,
                            lang_map, organize, verify, progress)

        try:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                for result in pool.map(run_one, files):
                    if result is None:
                        continue
                    self.queue.put(("result", result))
        except Exception as exc:  # never leave the UI spinning
            self.queue.put(("fatal", {"error": "%s: %s" % (type(exc).__name__, exc)}))
        finally:
            self.queue.put(("done", {}))

    def cancel(self):
        self.cancel_flag.set()
        self._append_log(self.t("stopping"))
        self.btn_cancel.setEnabled(False)

    # -------------------------------------------------------------- updates
    def _drain_queue(self):
        drained = 0
        while drained < 60:
            try:
                kind, payload = self.queue.get_nowait()
            except Empty:
                break
            drained += 1
            if kind == "result":
                self._handle_result(payload)
            elif kind == "stage":
                self._handle_stage(payload)
            elif kind == "update":
                self._on_update_result(payload)
            elif kind == "fatal":
                self._append_log(self.t("fatal") + payload.get("error", ""))
            elif kind == "done":
                self._drain_queue()
                self._finish()

    def _handle_stage(self, payload):
        names = {"metadata": self.t("stage_metadata"), "decrypt": self.t("stage_decrypt"),
                 "cover": self.t("stage_cover"), "tag": self.t("stage_tag"),
                 "verify": self.t("stage_verify")}
        self.lbl_stat.setText(self.t("processing") % (
            payload["name"][:44], names.get(payload["stage"], payload["stage"])))

    def _handle_result(self, r):
        self.done += 1
        if r["ok"]:
            self.ok += 1
            self.bytes_done += r["bytes"]
            self._append_log("✓ %s  →  %s  (%s)" % (
                r["name"][:38], os.path.basename(r["out"])[:38], self._human(r["bytes"])))
        else:
            self.failed += 1
            self._append_log(self.t("item_failed") % (r["name"][:38], r["error"]))
        self.bar.setValue(int(self.done / max(1, self.total) * 1000))
        elapsed = max(0.001, time.time() - self.started_at)
        speed = self.bytes_done / elapsed
        self.lbl_stat.setText(self.t("progress") % (
            self.done, self.total, self.ok, self.failed))
        self.lbl_speed.setText("%s/s" % self._human(speed))

    def _finish(self):
        self.btn_go.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.drop.setEnabled(True)
        elapsed = time.time() - self.started_at
        self._append_log(self.t("done_summary") % (self.ok, self.failed, elapsed))
        self.lbl_stat.setText(self.t("done_stat") % (
            self.done, self.total, self.ok, self.failed, elapsed))
        self.files = []
        self.drop.title.setText(self.t("drop_title"))
        self.drop.hint.setText(self.t("drop_hint"))
        if self.ok and not self.failed:
            self.bar.setValue(1000)


def _log_dir() -> str:
    """Where startup breadcrumbs go.

    NOT next to the source/EXE: that would drop a file containing absolute paths and the
    command line straight into the repository (a real leak we hit during review). Windows
    and macOS have a per-user log location for exactly this; the rest of the time a temp
    directory is fine.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "NCMConverter")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/NCMConverter")
    return os.path.join(os.environ.get("XDG_STATE_HOME",
                                       os.path.expanduser("~/.local/state")), "ncmconverter")


def _redact(text: str) -> str:
    """Strip anything machine-specific before it reaches a log file."""
    home = os.path.expanduser("~")
    for known in (home, os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", ""),
                  os.environ.get("TEMP", "")):
        if known:
            text = text.replace(known, "%USER%")
    # Keep the flag NAME, redact only its value: knowing which switches were used is the
    # point of a breadcrumb, while the value is what carries a path. The lookbehind accepts
    # a quote as well as whitespace so a repr()-ed argv is handled too.
    text = re.sub(r"(?<=[\s'\"])--([\w-]+)=[^\s'\"]+", r"--\1=<redacted>", text)
    # A verbatim absolute path can also reach a log inside an exception message, where no
    # --flag= prefix exists to key off. Redact the whole path rather than just one form.
    text = re.sub(r"[A-Za-z]:[\\/][^\s'\"]*", "%PATH%", text)
    return text


def _log_file(name: str) -> str:
    """Path of a diagnostic log file, in the first writable location.

    Falling back matters: in a locked-down environment the per-user log directory can be
    unwritable, and silently losing the breadcrumbs would leave a windowed build with no
    diagnostics at all. The temp directory is the last resort.
    """
    import tempfile

    candidates = [_log_dir(), os.path.join(tempfile.gettempdir(), "NCMConverter")]
    for directory in candidates:
        try:
            os.makedirs(directory, exist_ok=True)
            probe = os.path.join(directory, ".write-test")
            with open(probe, "w", encoding="utf-8"):
                pass
            os.remove(probe)
            return os.path.join(directory, name)
        except OSError:
            continue
    return os.path.join(candidates[-1], name)


def _log_path() -> str:
    """Full path of the startup log."""
    return _log_file("ncm_gui_startup.log")


def _boot_log(msg: str) -> None:
    """Append a startup breadcrumb to the user log directory.

    A windowed (console-less) build shows no traceback at all, so a crash on startup is
    otherwise invisible; this is the only cheap way to see how far it got. The message is
    redacted and the file never lands in the project tree.
    """
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), _redact(msg)))
    except Exception:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _boot_log("main() start flags=%r frozen=%s"
              % ([a for a in argv[1:] if a.startswith("--")], getattr(sys, "frozen", False)))
    selftest = "--selftest" in argv
    debug = bool(os.environ.get("NCM_GUI_DEBUG"))
    if debug:
        import atexit

        def trace(msg):
            with open(_log_file("gui_trace.log"), "a", encoding="utf-8") as fh:
                fh.write("%s pid=%d %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), _redact(msg)))

        def _bye():
            trace("exiting")
        atexit.register(_bye)
        trace("main() start, argv=%r" % (argv,))
    else:
        def trace(msg):
            pass

    app = QApplication(argv)
    app.setApplicationName("NCM Converter")
    # The window applies its own stylesheet: it owns the 日间/夜间/跟随系统 choice.
    app.setWindowIcon(make_icon())
    trace("QApplication ready")
    _boot_log("QApplication ready")
    try:
        window = MainWindow()
    except Exception as exc:
        import traceback
        _boot_log("MainWindow FAILED: %s\n%s" % (exc, traceback.format_exc()))
        raise
    trace("MainWindow constructed")
    _boot_log("MainWindow constructed, selftest=%s" % selftest)
    if selftest:
        # construct everything, exercise the state machine, then exit without a display.
        # A windowed (console-less) build cannot print anything, so --report=FILE exists
        # to let an automated check inspect the result.
        report = None
        for arg in argv:
            if arg.startswith("--report="):
                report = arg.split("=", 1)[1]
        window.add_paths([])
        from ncmdump.ncm_core import _numpy, xor_keystream
        lines = [
            "version         = %s" % VERSION,
            "developer       = %s" % DEVELOPER,
            "frozen          = %s" % getattr(sys, "frozen", False),
            "executable      = %s" % sys.executable,
            "python          = %s" % sys.version.split()[0],
            "PySide6 ok      = True",
            "numpy available = %s" % (_numpy() is not None),
            "keystream ok    = %s" % (xor_keystream(b"\x00\x01\x02\x03", bytes(256)) == b"\x00\x01\x02\x03"),
            "widgets         = %d" % sum(1 for _ in window.findChildren(QWidget)),
            "organize mode   = %s" % window.cmb_organize.currentText(),
            "drop accepts    = %s" % window.drop.acceptDrops(),
            "native frame    = %s" % bool(window.windowFlags() & Qt.WindowTitleHint),
            "material        = %s" % window._material,
            "pywinstyles     = %s" % (getattr(_pws, "__version__", "missing")
                                      if _pws is not None else "missing"),
            "icon ok         = %s" % (not window.windowIcon().isNull()),
        ]
        text = "\n".join(lines)
        _boot_log("selftest report:\n" + text)
        if report:
            try:
                with open(report, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except Exception as exc:
                _boot_log("report write failed: %s" % exc)
        try:
            print("selftest:\n" + text, flush=True)
        except Exception:
            pass
        return 0
    window.show()
    trace("shown; material=%s" % window._material)
    shot = None
    for arg in argv:
        if arg.startswith("--screenshot="):
            shot = arg.split("=", 1)[1]
    if shot:
        # render the live window to a PNG and quit: lets the UI be checked without a human
        def snap():
            try:
                path = shot.replace("{hwnd}", str(int(window.winId())))
                window.grab().save(path)
                trace("screenshot saved to %s" % path)
            except Exception as exc:
                trace("screenshot failed: %s" % exc)
            app.quit()

        QTimer.singleShot(1200, snap)
    code = app.exec()
    trace("event loop returned %d" % code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
