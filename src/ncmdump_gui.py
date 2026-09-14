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

    pywinstyles' own ``paint()`` helper sets the *whole* widget stylesheet to
    ``background-color: transparent`` before enabling acrylic — which would also wipe the
    app's styling. We instead give the window object a transparent background ourselves, so
    Qt leaves the client area unpainted and the DWM material shows through.
    """
    if _pws is None or os.name != "nt":
        return "none"
    hwnd = int(window.winId())
    if mode == "solid":
        _pws.apply_style(hwnd, "normal")
        return "solid"
    try:
        style = mode if mode in ("acrylic", "mica", "aero") else "acrylic"
        _pws.apply_style(hwnd, style)
        return style
    except Exception as exc:
        _boot_log("pywinstyles failed (%s): %s" % (mode, exc))
        return "none"


def window_stylesheet(glass_on: bool) -> str:
    """Global styling plus a rule that keeps the window's own background transparent.

    The extra rule is *appended* to the app stylesheet rather than set on its own:
    ``setStyleSheet`` replaces, so assigning just that one rule would strip every other
    style in the app.
    """
    extra = ""
    if glass_on:
        extra = "\nQWidget#root { background: transparent; }\n"
    return STYLE + extra


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


def enable_shadow(hwnd: int) -> None:
    """Give the frameless window a shadow so it does not look pasted onto the desktop."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class MARGINS(ctypes.Structure):
            _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                        ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

        margins = MARGINS(1, 1, 1, 1)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            wintypes.HWND(hwnd), ctypes.byref(margins))
    except Exception:
        pass


# --- Qt ---------------------------------------------------------------------

from PySide6.QtCore import QRectF, QStandardPaths, Qt, QTimer, Signal  # noqa: E402
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
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
QWidget { color: #eaf0ff; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; }
#root { background: transparent; }
/* No borders inside the app: separation comes from fill contrast and rounded light
   surfaces, so the design reads as one continuous sheet of glass. */
#card {
    background: rgba(14, 18, 30, 58);
    border: none;
    border-radius: 14px;
}
#card[solid="true"] {
    background: rgba(22, 26, 38, 246);
}
/* Native title bar is drawn by Windows; this strip is just an in-app header. */
#appheader { background: transparent; }
#title { font-size: 15px; font-weight: 600; letter-spacing: 0.4px; }
#subtitle { color: rgba(232, 240, 255, 225); font-size: 11px; }
#drop {
    background: rgba(255, 255, 255, 40);
    border: none;
    border-radius: 14px;
}
#drop[hot="true"] { background: rgba(130, 190, 255, 104); }
#dropTitle { font-size: 15px; font-weight: 600; color: #f5f9ff; }
#dropHint { color: rgba(228, 238, 255, 200); font-size: 12px; }
#sectionTitle { color: rgba(212, 224, 252, 225); font-size: 11px; font-weight: 600; letter-spacing: 1px; }
QPushButton#ghost {
    background: rgba(255, 255, 255, 46); border: none;
    border-radius: 9px; padding: 8px 15px;
}
QPushButton#ghost:hover { background: rgba(255, 255, 255, 78); }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(86, 182, 255, 240), stop:1 rgba(158, 118, 255, 240));
    border: none; border-radius: 12px; padding: 12px 26px;
    font-size: 14px; font-weight: 700; color: #0b1020;
}
QPushButton#primary:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(120, 200, 255, 252), stop:1 rgba(180, 145, 255, 252)); }
QPushButton#primary:disabled { background: rgba(255, 255, 255, 40); color: rgba(235, 240, 255, 150); }
QPushButton#danger {
    background: rgba(255, 92, 104, 205); border: none; border-radius: 12px;
    padding: 12px 20px; font-weight: 700; color: white;
}
QPushButton#danger:hover { background: rgba(255, 116, 126, 235); }
QPushButton#danger:disabled { background: rgba(255, 255, 255, 30); color: rgba(235, 240, 255, 120); }
QLineEdit {
    background: rgba(255, 255, 255, 44); border: none;
    border-radius: 9px; padding: 8px 11px; selection-background-color: rgba(120, 180, 255, 150);
}
QLineEdit:focus { background: rgba(255, 255, 255, 64); }
QLineEdit:disabled { color: rgba(220, 228, 250, 120); background: rgba(255, 255, 255, 22); }
QCheckBox { spacing: 8px; color: rgba(236, 242, 255, 245); }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 5px;
                       border: none; background: rgba(255, 255, 255, 58); }
QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 rgba(96, 190, 255, 250), stop:1 rgba(160, 120, 255, 250));
}
QProgressBar {
    background: rgba(255, 255, 255, 32); border: none; border-radius: 7px;
    height: 14px; text-align: center; color: rgba(245, 248, 255, 245); font-size: 10px;
}
QProgressBar::chunk {
    border-radius: 7px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(86, 182, 255, 245), stop:1 rgba(158, 118, 255, 245));
}
#log {
    background: rgba(255, 255, 255, 26); border: none;
    border-radius: 10px; padding: 9px; color: rgba(232, 240, 255, 245);
    font-family: Consolas, "Cascadia Mono", monospace; font-size: 11px;
}
#stat { color: rgba(232, 240, 255, 240); font-size: 12px; }
#statAccent { color: #a8dcff; font-weight: 600; }
QScrollArea { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 80); border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


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
        self.title = QLabel("把 .ncm 文件或文件夹拖到这里")
        self.title.setObjectName("dropTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.hint = QLabel("也可以点下面的按钮选择；支持递归子目录")
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
    """Slim in-app header.

    There is deliberately no custom title bar any more: the window keeps the **native**
    Windows frame, so dragging, resizing, Aero Snap, double-click-to-maximise, the system
    menu and Alt+Space all come from the OS instead of being re-implemented (badly) here.
    This strip only carries the app name, the material badge and the material toggle.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("appheader")
        self.setFixedHeight(40)


class GlassBackdrop(QWidget):
    """Hosts the window contents and lets the native blurred backdrop show through.

    The window itself carries no background now: ``pywinstyles.apply_style(hwnd,
    "acrylic")`` makes Qt paint the client area transparent and extends the DWM frame over
    the whole window, so Windows composites its blurred material edge to edge. This widget
    therefore only paints the rounded shape and the four corner cut-outs; everything else
    is the OS material seen through the window.
    """

    RADIUS = 16

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


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        from ncmdump.ncm2mp3 import find_inputs, load_lyrics, split_lrc, sanitize, human  # noqa
        self._sanitize = sanitize
        self._human = human
        self._find_inputs = find_inputs

        # Native window frame on purpose: keep the OS title bar, resizable borders and all
        # the shell behaviour that comes with them (snap, maximise, system menu).
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint
                            | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setObjectName("root")
        self.setWindowTitle("NCM 本地转换器")
        self.setWindowIcon(make_icon())
        self.resize(940, 700)
        self.setMinimumSize(760, 560)

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

        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._drain_queue)

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
        overlay.setContentsMargins(14, 14, 14, 14)
        overlay.setSpacing(0)

        card = QFrame()
        card.setObjectName("card")
        self.card = card
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(26)
        shadow.setColor(QColor(0, 0, 0, 90))
        shadow.setOffset(0, 4)
        card.setGraphicsEffect(shadow)
        overlay.addWidget(card)

        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(12)

        # ---- in-app header (the OS title bar sits above this; no window buttons here)
        self.header = AppHeader(card)
        bar = QHBoxLayout(self.header)
        bar.setContentsMargins(2, 0, 2, 0)
        bar.setSpacing(8)
        dot = QLabel("◍")
        dot.setStyleSheet("color: #7fc4ff; font-size: 16px;")
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        self.lbl_title = QLabel("NCM 本地转换器")
        self.lbl_title.setObjectName("title")
        self.lbl_sub = QLabel("离线解密 · 保留标签与歌词 · 纯本地")
        self.lbl_sub.setObjectName("subtitle")
        title_box.addWidget(self.lbl_title)
        title_box.addWidget(self.lbl_sub)
        bar.addWidget(dot)
        bar.addLayout(title_box)
        bar.addStretch(1)
        outer.addWidget(self.header)

        # ---- drop zone
        self.drop = DropZone()
        self.drop.filesDropped.connect(self.add_paths)
        outer.addWidget(self.drop)

        # ---- output row
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("输出目录")
        lbl.setObjectName("sectionTitle")
        self.ed_out = QLineEdit(os.environ.get("NCM_OUT_DIR", ""))
        self.ed_out.setPlaceholderText("留空 = 转换到每个 .ncm 所在目录")
        self.btn_out = QPushButton("选择…")
        self.btn_out.setObjectName("ghost")
        self.btn_out.clicked.connect(self.pick_output)
        row.addWidget(lbl)
        row.addWidget(self.ed_out, 1)
        row.addWidget(self.btn_out)
        outer.addLayout(row)

        # ---- organize row
        org = QHBoxLayout()
        org.setSpacing(8)
        self.chk_organize = QCheckBox("按语言分目录")
        self.chk_organize.setChecked(True)
        self.chk_organize.toggled.connect(self._toggle_organize)
        self.ed_ja = QLineEdit("VIP(Japanness)")
        self.ed_other = QLineEdit("VIP(Other language)")
        org.addWidget(self.chk_organize)
        org.addWidget(QLabel("日文 →"))
        org.addWidget(self.ed_ja, 1)
        org.addWidget(QLabel("其他 →"))
        org.addWidget(self.ed_other, 1)
        outer.addLayout(org)

        # ---- options
        opts = QHBoxLayout()
        opts.setSpacing(16)
        self.chk_keep = QCheckBox("保留 .ncm")
        self.chk_lyrics = QCheckBox("嵌入歌词")
        self.chk_lyrics.setChecked(True)
        self.chk_verify = QCheckBox("删除前逐字节校验")
        self.chk_verify.setChecked(True)
        self.chk_cover = QCheckBox("下载封面")
        for widget in (self.chk_keep, self.chk_lyrics, self.chk_verify, self.chk_cover):
            opts.addWidget(widget)
        opts.addStretch(1)
        outer.addLayout(opts)

        # ---- action row
        action = QHBoxLayout()
        action.setSpacing(10)
        self.btn_go = QPushButton("开始转换")
        self.btn_go.setObjectName("primary")
        self.btn_go.clicked.connect(self.start)
        self.btn_cancel = QPushButton("停止")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_open = QPushButton("打开输出目录")
        self.btn_open.setObjectName("ghost")
        self.btn_open.clicked.connect(self.open_output)
        action.addWidget(self.btn_go, 2)
        action.addWidget(self.btn_cancel, 1)
        action.addWidget(self.btn_open, 1)
        outer.addLayout(action)

        # ---- progress
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        outer.addWidget(self.bar)
        stats = QHBoxLayout()
        self.lbl_stat = QLabel("等待任务")
        self.lbl_stat.setObjectName("stat")
        self.lbl_speed = QLabel("")
        self.lbl_speed.setObjectName("statAccent")
        stats.addWidget(self.lbl_stat, 1)
        stats.addWidget(self.lbl_speed)
        outer.addLayout(stats)

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
        outer.addWidget(scroll, 1)

        self._toggle_organize(True)

    # ----------------------------------------------------------- window chrome
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
        try:
            enable_shadow(hwnd)
        except Exception:
            pass

        # Native window material through pywinstyles (acrylic by default), falling back to
        # the raw DWM backdrop API if the package is missing.
        self.setStyleSheet(window_stylesheet(self._glass_on))
        applied = self._material
        if not self._glass_on:
            pywinstyles_normal(hwnd)
        else:
            applied = apply_window_style(self, self._material)
            if applied == "none":
                applied = apply_glass(hwnd, True)
        self._material = applied
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
    def _toggle_organize(self, on: bool):
        self.ed_ja.setEnabled(on)
        self.ed_other.setEnabled(on)

    def pick_output(self):
        current = self.ed_out.text().strip()
        if not current:
            # no personal path baked in: start from the user's own Music folder
            current = QStandardPaths.writableLocation(QStandardPaths.MusicLocation) \
                or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", current)
        if chosen:
            self.ed_out.setText(chosen)
            self._append_log("输出目录：" + chosen)

    def open_output(self):
        path = self.ed_out.text().strip()
        if not path:
            path = os.path.dirname(os.path.abspath(self.files[0])) if self.files else ""
        if not path or not os.path.isdir(path):
            self._append_log("输出目录不存在：%s（未指定时输出在源文件所在目录）"
                             % (path or "(空)"))
            return
        try:
            os.startfile(path)  # noqa: S606  (Windows shell open)
        except Exception as exc:
            self._append_log("打开失败：%s" % exc)

    def add_paths(self, paths):
        found = self._find_inputs(paths, recursive=True)
        known = {os.path.abspath(p).lower() for p in self.files}
        added = [p for p in found if os.path.abspath(p).lower() not in known]
        self.files.extend(added)
        total_bytes = sum(os.path.getsize(p) for p in self.files if os.path.isfile(p))
        self.drop.title.setText("已选择 %d 个 .ncm（%s）" % (len(self.files), self._human(total_bytes)))
        self.drop.hint.setText("可以继续拖入更多文件；点“开始转换”即可")
        self.lbl_stat.setText("待转换 %d 个文件 · %s" % (len(self.files), self._human(total_bytes)))
        if added:
            self._append_log("已加入 %d 个文件" % len(added))
        else:
            self._append_log("没有找到新的 .ncm 文件")

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
            roots.add(os.path.abspath(out_dir))
            if self.chk_organize.isChecked():
                for sub in (self.ed_ja.text().strip(), self.ed_other.text().strip()):
                    if sub:
                        roots.add(os.path.join(os.path.abspath(out_dir), sub))
        for root in roots:
            try:
                for name in os.listdir(root):
                    if not (name.startswith(".") and name.endswith(".ncmtmp")):
                        continue
                    path = os.path.join(root, name)
                    try:
                        if os.path.getmtime(path) > cutoff:
                            continue          # possibly a running conversion
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
            except OSError:
                continue
        return removed

    def start(self):
        if not self.files:
            self._append_log("请先拖入或选择 .ncm 文件")
            return
        out_dir = self.ed_out.text().strip()
        # an empty box means "next to each source file"; only create it when it is set,
        # and never fabricate a machine-specific default path
        if out_dir:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                self._append_log("输出目录不可用：%s" % exc)
                return

        stale = self._clean_stale_tempfiles(out_dir)
        if stale:
            self._append_log("清理了 %d 个上次中断残留的临时文件" % stale)

        lang_map = None
        if self.chk_organize.isChecked():
            lang_map = {"ja": self.ed_ja.text().strip() or "ja",
                        "other": self.ed_other.text().strip() or "other"}
            if not lang_map["other"]:
                self._append_log("“其他”目录名不能为空")
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
        self._append_log("开始转换 %d 个文件 → %s" % (self.total, out_dir))

        keep = self.chk_keep.isChecked()
        want_lyrics = self.chk_lyrics.isChecked()
        verify = self.chk_verify.isChecked()
        want_cover = self.chk_cover.isChecked()
        jobs = max(2, min(6, (os.cpu_count() or 4)))

        self.timer.start()
        thread = threading.Thread(
            target=self._worker,
            args=(list(self.files), out_dir, keep, want_cover, want_lyrics, verify,
                  lang_map, jobs),
            daemon=True,
        )
        thread.start()

    def _worker(self, files, out_dir, keep, want_cover, want_lyrics, verify, lang_map, jobs):
        from ncmdump.ncm2mp3 import _convert

        def run_one(src):
            if self.cancel_flag.is_set():
                return None
            name = os.path.basename(src)

            def progress(stage, fraction=0.0):
                self.queue.put(("stage", {"name": name, "stage": stage, "fraction": fraction}))

            return _convert(src, out_dir, keep, want_cover, want_lyrics, False,
                            lang_map, bool(lang_map), verify, progress)

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
        self._append_log("正在停止…（已开始的当前文件会跑完）")
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
            elif kind == "fatal":
                self._append_log("致命错误：" + payload.get("error", ""))
            elif kind == "done":
                self.timer.stop()
                self._drain_queue()
                self._finish()

    def _handle_stage(self, payload):
        names = {"metadata": "读取信息", "decrypt": "解密音频", "cover": "下载封面",
                 "tag": "写入标签", "verify": "校验"}
        self.lbl_stat.setText("正在处理：%s  ·  %s" % (
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
            self._append_log("✗ %s  失败：%s" % (r["name"][:38], r["error"]))
        self.bar.setValue(int(self.done / max(1, self.total) * 1000))
        elapsed = max(0.001, time.time() - self.started_at)
        speed = self.bytes_done / elapsed
        self.lbl_stat.setText("完成 %d/%d · 成功 %d · 失败 %d" % (
            self.done, self.total, self.ok, self.failed))
        self.lbl_speed.setText("%s/s" % self._human(speed))

    def _finish(self):
        self.btn_go.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.drop.setEnabled(True)
        elapsed = time.time() - self.started_at
        self._append_log("完成：成功 %d，失败 %d，用时 %.1fs" % (self.ok, self.failed, elapsed))
        self.lbl_stat.setText("完成 %d/%d · 成功 %d · 失败 %d · 用时 %.1fs" % (
            self.done, self.total, self.ok, self.failed, elapsed))
        self.files = []
        self.drop.title.setText("把 .ncm 文件或文件夹拖到这里")
        self.drop.hint.setText("也可以点下面的按钮选择；支持递归子目录")
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
    # keep only the flag names from the command line, never the values (they carry paths)
    text = re.sub(r"(?<=\s)--[\w-]+=[^\s'\"]+", "--<redacted>", text)
    return text


def _log_path() -> str:
    """Full path of the startup log, in the first writable location.

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
            return os.path.join(directory, "ncm_gui_startup.log")
        except OSError:
            continue
    return os.path.join(candidates[-1], "ncm_gui_startup.log")


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
            with open("ncm-work/gui_trace.log", "a", encoding="utf-8") as fh:
                fh.write("%s pid=%d %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), msg))

        def _bye():
            trace("exiting")
        atexit.register(_bye)
        trace("main() start, argv=%r" % (argv,))
    else:
        def trace(msg):
            pass

    app = QApplication(argv)
    app.setApplicationName("NCM 本地转换器")
    app.setStyleSheet(STYLE)
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
            "frozen          = %s" % getattr(sys, "frozen", False),
            "executable      = %s" % sys.executable,
            "python          = %s" % sys.version.split()[0],
            "PySide6 ok      = True",
            "numpy available = %s" % (_numpy() is not None),
            "keystream ok    = %s" % (xor_keystream(b"\x00\x01\x02\x03", bytes(256)) == b"\x00\x01\x02\x03"),
            "widgets         = %d" % sum(1 for _ in window.findChildren(QWidget)),
            "organize on     = %s" % window.ed_ja.isEnabled(),
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
