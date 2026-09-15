# 毛玻璃/透明度排查手册

> 为什么"代码明明开了透明"看起来还是不透明 —— 以及怎么用像素证明它到底透没透。
>
> 本文所有结论都在 Windows 11（build 22631）+ PySide6 6.9.3 + pywinstyles 1.8 上实测过，
> 附具体像素值，不是推断。

---

## 零、先记住这一条

**`QWidget.grab()` / `QScreen.grabWindow()` 不能用来判断毛玻璃是否生效。**

Qt 画的是"应用程序自己的图层"，而毛玻璃是 **DWM 合成器**在窗口背后做的。
两者不是一回事：自截图里背景可能是全黑、全透明或一片灰，而屏幕上明明是好看的半透明玻璃。

我们在这个项目里就因此走了很久弯路。下面是同一窗口、同一位置的对比：

| 采样位置 | 程序自截 `win.grab()` | 屏幕实拍 (`ImageGrab`) |
| --- | --- | --- |
| 窗口下方空白 | `(10, 14, 24, alpha=106)` | `(23, 32, 53)` |
| 上部背景 | `(88, 90, 99, alpha=136)` | `(18, 28, 50)` |

看起来"完全不一样"。但把自截图的 **premultiplied alpha** 还原后做合成验算：

```
自截 (10,14,24, a=106)  →  还原直色 (24,34,58)，alpha = 0.42
屏幕 = 直色 × 0.42 + 背景 × 0.58
  →  反推背景 = (22, 31, 50)
屏幕实拍                  = (23, 32, 53)     ← 几乎完全一致
```

**结论：窗口在屏幕上确实是半透明的（约 58% 的背景透过来），而自截图把它显示成了不透明。**
自截图不是"真相"，它只是"程序画了什么"。

> 注意 `QWidget.grab()` 返回的 RGB 是**已乘过 alpha** 的（premultiplied）。
> 想拿到"程序实际画的颜色"，必须 `rgb * 255 / alpha` 还原，否则会得到一片灰扑扑的错误结论。

---

## 一、透明是三层状态

要让整窗毛玻璃生效，需要三件事同时成立。**但要注意第 ③ 层是否必要，取决于你用的材质**，
不能一概而论（下一节有实测）。

| 层 | 作用 | 典型 API | 不满足时的现象 |
| --- | --- | --- | --- |
| ① 窗口本体 | 让客户区**不被 Qt 画满** | 样式表把窗口背景设为 `transparent`（或 `WA_TranslucentBackground`） | 玻璃被自己的不透明底盖住，完全看不到 |
| ② 材质 | 告诉 DWM "这个窗口要模糊" | pywinstyles `apply_style(hwnd, "aero")`；或 `SetWindowCompositionAttribute` | 窗口是透明的，但背后**没有模糊**，只是普通透明 |
| ③ 框架延伸 | 把材质**铺满整个客户区** | `DwmExtendFrameIntoClientArea(hwnd, -1)` | 只有标题栏那一圈有材质，客户区是死板的实色——**只对部分材质成立，见下** |

### ③ 到底需不需要？看材质，别照抄

查 pywinstyles 1.8 的源码（`py_win_style.py`），`apply_style` 各分支实际调用：

| style | 调用 |
| --- | --- |
| `mica` | `ChangeDWMAttrib(19,1)` + `ChangeDWMAttrib(1029,1)` |
| `aero` | `paint()` + `ChangeDWMAccent(30,2)` + `ChangeDWMAccent(19,3)` |
| `acrylic` | `paint()` + `ChangeDWMAttrib(20,1)` + `ChangeDWMAccent(30,3)` + **`ExtendFrameIntoClientArea`** |
| `normal` | 一串 `ChangeDWMAccent`/`ChangeDWMAttrib` 归零 |

**只有 `acrylic` 分支调了 `ExtendFrameIntoClientArea`。** 那 `aero` 是不是就没铺满？

实测（同一程序、同一窗口，用 `NCM_GLASS` 切换材质，只在**客户区**内采样，
沿四条中线取平均）：

| 距客户区边缘 | 1px | 3px | 8px | 20px | 40px | 中心 |
| --- | --- | --- | --- | --- | --- | --- |
| **aero** | (30,40,67) | (29,40,67) | (29,40,66) | (25,34,56) | (37,45,66) | (39,46,65) |
| **acrylic** | (17,23,37) | (17,23,37) | (17,23,36) | (16,21,34) | (29,34,46) | (29,34,46) |

**结论：两种材质的边缘与中心差异都只有 13~14，都在噪声范围内——材质本来就铺满整个客户区。**
`aero` 不需要那次显式延伸调用。

> 所以"必须调 `ExtendFrameIntoClientArea` 否则只有标题栏有材质"是一个**常见但错误的经验**。
> 它来自早期手工调用 `SetWindowCompositionAttribute` 的时代，不是 `apply_style` 的行为。
> 自己手写 DWM 调用时才需要注意框架延伸（见坑 6）。

**顺带一个有用的观察**：`aero` 整体明显比 `acrylic` **亮**（同位置 39 vs 29）。
想要"模糊更明显、更亮"的效果用 aero；想要"更暗更沉"的用 acrylic。这不是好坏问题，是取色不同。

> ⚠️ **采样必须在客户区内做。** 窗口矩形（`GetWindowRect`）包含系统标题栏和可拉伸边框，
> 直接采窗口矩形会拿"标题栏 vs 内容"作比较，得出的差异毫无意义——我第一次就踩了这个坑，
> 得到"边缘差 51"的错误结论。正确做法是 `GetClientRect` + `ClientToScreen` 换算后再截。

---

## 二、这个项目踩过的 6 个坑（按坑人程度排序）

### 坑 1：用自截图/`grabWindow` 判断材质，得出完全错误的结论 ⭐最常见

**现象**：截图全黑或全透明，以为材质没生效；其实屏幕上一直是好的。
**原因**：见第零节。
**正确做法**：只信"屏幕上真实合成出来的像素"。

```python
from PIL import ImageGrab
shot = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)   # ✅ 真实合成结果
# win.grab()                                                        # ❌ 只是程序自己的图层
```

`QScreen.grabWindow()` 在部分驱动/Windows 版本上会返回**壁纸**而不是窗口（它是从屏幕缓冲区拷的），
同样不可靠。

---

### 坑 2：无边框 + 圆角卡片 + 没画底板 → 四周一圈透明

**现象**：窗口外面套了一圈透明环，看起来像"底板不透明、外圈漏了"。
**原因**：为了圆角，窗口设了 `WA_TranslucentBackground`，然后卡片留了 14px 边距居中。
   - 那 14px 区域**没有任何东西绘制**：既不是卡片，也不是窗口背景（它是透明的）。
   - 于是那一圈就是纯透明——在深色壁纸上看起来是"一圈特别黑的边"。

```
┌──────────────────────────────┐
│ ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒ │ ← 这圈没人画 = 纯透明
│ ▒┌────────────────────────┐▒ │
│ ▒│      卡片（不透明）      │▒ │
│ ▒└────────────────────────┘▒ │
│ ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒ │
└──────────────────────────────┘
```

**修法**：让**整窗都有东西画**——用 `WA_TranslucentBackground` 时，窗口级 widget 必须自己填满
（渐变/遮色/圆角底板都可以），不能留白。

判断标准（可直接拿来当验收）：

```python
# 除窗口圆角外，客户区内不应存在 alpha 明显偏低的像素
alphas = [img.getpixel((x, y))[3] for x, y in sample_points_inside_pane]
assert min(alphas) > 250, "面板内出现了透明像素 = 有地方没画"
```

---

### 坑 3：卡片铺满整个窗口 → 材质"消失"

**现象**：`apply_style(hwnd, "aero")` 明明返回成功，窗口却完全不透。
**原因**：材质在**窗口背后**，而卡片是不透明的深色面板。卡片盖住了整个客户区，
  就等于在玻璃上糊了一张纸。

**修法**：两者取一——
- 卡片做成**半透明薄色**（本项目用 `rgba(14,18,30,58)`），让材质透出来；
- 或者干脆不要卡片，整窗只留一层很薄的遮色（本项目最终采用：整窗 Aero + 一层
  `alpha=46` 的遮色）。

**遮色 alpha 的取舍**：越大越"实"、越稳，但越看不见玻璃。46 是我们实测下来
既保证文字可读、又能看出模糊的值；72 就已经明显发闷了。

---

### 坑 4：Mica 在深色壁纸上"看起来像没效果"

**现象**：Mica 开了，但窗口是一块很暗的蓝灰，像坏了。
**原因**：**Mica 的颜色来自壁纸**。本项目开发机壁纸均值是 `RGB(66,82,107)`，
  映射出来就是一片暗蓝灰——材质其实**工作正常**，只是不好看。

**验证方法**（证明它真的在取壁纸色）：把窗口挪到壁纸的不同区域，采样屏幕像素：

```
窗口在壁纸亮区 → 采样偏亮
窗口在壁纸暗区 → 采样偏暗
```

如果像素跟着位置变，材质就是生效的，问题只是审美而不是功能。

**结论**：想要"明显的模糊玻璃"，用 **Aero**（模糊更强、不依赖壁纸取色）；
Mica 适合配合浅色/彩色壁纸。**别把"不好看"当成"没生效"去 debug。**

---

### 坑 5：`paint()` 把整个样式表清空

**现象**：材质调完之后，界面所有自定义样式全没了。
**原因**：`apply_style()` 对 `aero` / `acrylic` / `transparent` 这几个 style 会先调内部的
`paint()`，而它在 PyQt/PySide 分支里的实现就一句：

```python
def paint(window):
    try:      # tkinter
        window.config(bg="black"); return
    except:   pass
    try:      # pyqt/pyside
        window.setStyleSheet("background-color: transparent;"); return   # ← 就是这里
    except:   pass
    try:      # wxpython
        window.SetBackgroundColour("black"); return
    except:   pass
    print("Don't know what the window type is, please paint it black")
```

`setStyleSheet()` 是**替换**语义——它会把你之前设的整张样式表覆盖成这一条。

**修法**：不要依赖 `apply_style` 帮你处理窗口背景。把自己的样式表设好之后，
再**追加**"窗口背景透明"这一条（`setStyleSheet` 替换，但字符串可以拼接）：

```python
def window_stylesheet(glass_on: bool) -> str:
    extra = "\nQWidget#root { background: transparent; }\n" if glass_on else ""
    return STYLE + extra      # 追加，不是替换
```

并且给窗口 `setObjectName("root")` 才能被这条规则选中。
**同时注意调用顺序**：先 `apply_style`（它可能覆盖样式表），**再** `setStyleSheet(拼接好的)`。

**顺带解释一句噪音**：`apply_style` 在 Qt 窗口上通常不会打印
`Don't know what the window type is, please paint it black`——因为 PyQt 分支能命中。
如果看到了这句，说明 `window` 传进去的不是真实 widget（例如传了 `hwnd` 整数而不是窗口对象），
`paint()` 三个分支全都没命中，材质也就没正确设置背景。**这句话是个有用的报警，别忽略。**

---

### 坑 6：`SystemBackdropType` 单独用是没效果的

**现象**：调了 `DwmSetWindowAttribute(hwnd, 38, DWMSBT_MAINWINDOW)` 没反应。
**原因**：`DWMWA_SYSTEMBACKDROP_TYPE`（38）**必须配合**
  `DWMWA_USE_IMMERSIVE_DARK_MODE`（20）和框架延伸；单独设属性 DWM 会忽略。

本项目最终没有走这条路，而是交给 pywinstyles（它内部用的是 `ChangeDWMAccent`，
即 `SetWindowCompositionAttribute`，不是 38 号属性）。如果自己手写 38 号这条路，顺序建议：

```python
import ctypes
from ctypes import wintypes

class MARGINS(ctypes.Structure):        # 必须给 DWM 一个真实的 MARGINS 结构
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3              # 0=auto 1=none 2=mainwindow 3=transient 4=tabbed
dwm = ctypes.windll.dwmapi

dwm.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                          ctypes.byref(ctypes.c_int(1)), 4)      # 先声明深色 UI
dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(MARGINS(-1, -1, -1, -1)))
dwm.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                          ctypes.byref(ctypes.c_int(DWMSBT_TRANSIENTWINDOW)), 4)
```

> 注意：`MARGINS(-1,-1,-1,-1)` 是"整个窗口都是玻璃"，`0` 是"关闭"。传错方向会出现
> 全白/全黑边框或干脆没反应。

---

## 三、一份可直接抄的"正确姿势"

本项目的最终实现（`src/ncmdump_gui.py`），要点都标出来了：

```python
# ① 使用系统原生窗口：保留标题栏/可拉伸边框/Aero Snap，不要 FramelessWindowHint
self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint
                    | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)

# ② 窗口材质交给 pywinstyles（内部含框架延伸）
pywinstyles.apply_style(hwnd, "aero")

# ③ 标题栏/边框配色，并去掉窗口边框
pywinstyles.change_header_color(hwnd, "#141a28")
pywinstyles.change_title_color(hwnd, "#e3ebff")
DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)

# ④ 让被系统材质顶出来的部分"有东西画"：整窗一层薄遮色
#    遮色必须覆盖整个窗口，否则四周会出现坑 2 的透明环
self.backdrop.set_tint(QColor(8, 12, 22, 46))    # alpha 越小越透
```

**关于无边框窗口的取舍**：本项目**一度**用了 `FramelessWindowHint` 自绘标题栏，
代价是丢掉拉伸、吸附、双击最大化、系统菜单——而且**仍然需要**自己处理坑 2。
最终结论：**除非有强烈的视觉需求，别用无边框**。pywinstyles 直接作用在原生窗口上，
原生窗口 + 材质是又省事又不会出错的一条路。

---

## 四、排查清单（照着走一遍）

1. **先证明屏幕上到底透没透**——用 `ImageGrab`，不要用 `grab()`（第零节）。
   记得**采样要在客户区内**：`GetClientRect` + `ClientToScreen`，不要直接采窗口矩形。
2. **算一次合成**：`屏幕 = 程序直色 × alpha + 背景 × (1-alpha)`。
   自截图记得先 `rgb × 255 / alpha` 还原 premultiplied。
   - 算得通 → 透明是生效的，去查**审美**（遮色太厚 / Mica 取了暗色壁纸）。
   - 算不通 → 继续往下。
3. **确认材质调用成功且窗口对象传对了**：控制台若出现
   `Don't know what the window type is` 就说明 `paint()` 没命中，材质没生效。
4. **看是不是被自己的卡片盖住了**（坑 3）：把卡片底色临时调成 50% 透明试试。
5. **看四周是否有没人画的区域**（坑 2）：采样客户区**最边缘 1~3px** 的 alpha。
6. **确认样式表没被覆盖**（坑 5）：用的是追加还是替换？顺序对不对？
7. **确认材质真的被应用**：让程序把自己的材质状态写进日志
   （本项目是 `window: material=aero hwnd=... native_frame=True`），
   别靠肉眼猜。

---

## 五、参考：常用调用速查

| 目的 | 调用 |
| --- | --- |
| 整窗毛玻璃（模糊明显、偏亮） | `pywinstyles.apply_style(hwnd, "aero")` |
| 整窗毛玻璃（更暗更沉） | `pywinstyles.apply_style(hwnd, "acrylic")` |
| 从壁纸取色的静态材质 | `pywinstyles.apply_style(hwnd, "mica")` |
| 关闭材质 | `pywinstyles.apply_style(hwnd, "normal")` |
| 标题栏/标题文字配色 | `pywinstyles.change_header_color` / `change_title_color` |
| 窗口边框颜色 | `pywinstyles.change_border_color(hwnd, "#20293d")` |
| 去掉窗口边框 | `DwmSetWindowAttribute(hwnd, 34, 0xFFFFFFFE)`（`DWMWA_COLOR_NONE`） |
| 让材质铺满客户区（**手写 DWM 时才需要**） | `DwmExtendFrameIntoClientArea(hwnd, MARGINS(-1,-1,-1,-1))` |
| 系统背景材质（Win11，需配合属性 20） | `DwmSetWindowAttribute(hwnd, 38, DWMSBT_*)` |
| 传统模糊（Win10 兼容） | `SetWindowCompositionAttribute`，accent state 3=blur / 4=acrylic |
| 窗口透明度 | `pywinstyles.set_opacity(hwnd, 0.9)`（整窗，注意会连内容一起变） |

**取 hwnd**：PySide6 里 `int(window.winId())` 就是 HWND，pywinstyles 直接接受
（注意是传**窗口对象**给 `apply_style`，不是传整数）。

**调试用环境变量**：本项目支持 `NCM_GLASS=aero|acrylic|mica|solid` 在运行时切换材质，
排查时不必改代码重编译：

```powershell
$env:NCM_GLASS="acrylic"; .\NCMConverter.exe
```

---

## 六、一句话总结

> **毛玻璃不在你的窗口里，在窗口后面。**
> 你只能决定"让多少背景透过来"（遮色 alpha）和"要不要模糊"（材质调用），
> 而**唯一能证明结果的证据是屏幕上合成后的像素**——不是 `grab()`，也不是肉眼在深色壁纸上的观感。
>
> 另外：**先确认自己在量的是"客户区"，再下结论。** 我因为量了窗口矩形而不是客户区，
> 得出过一个完全错误的"边缘差 51"结论。

---

## 附：本文的证据脚本

产生本文数据的脚本留在仓库外的工作目录里，需要复现时可用：

| 脚本 | 作用 |
| --- | --- |
| `glass_evidence.py` | 抓同一窗口的 `grab()` 与屏幕实拍，供合成验算 |
| `coverage_test.py` | 用 `NCM_GLASS` 切换材质，在**客户区内**采样边缘 vs 中心，验证材质覆盖范围 |

核心方法只有两句，可以直接抄：

```python
# 真实合成结果（可信）
img = ImageGrab.grab(bbox=(x0, y0, x0 + cw, y0 + ch), all_screens=True)

# 客户区矩形（必须用它，不能用 GetWindowRect）
u.GetClientRect(hwnd, byref(client)); u.ClientToScreen(hwnd, byref(pt))
```
