# ncmdump-local

把网易云音乐的 `.ncm` 文件**在本地**转换成可播放的 FLAC/MP3，支持图形界面与命令行。

> Convert NetEase Cloud Music `.ncm` files to playable FLAC/MP3 **locally** — GUI and CLI.
> Pure-Python core with **no third-party runtime dependencies**, fully offline: nothing is
> uploaded and no account is involved. A prebuilt Windows build is attached to
> [Releases](https://github.com/Calvin-Vollerei/ncmdump-local/releases).

* **核心零第三方依赖** —— 纯 Python 标准库实现 AES、容器解析、FLAC/ID3 标签写入
* **全程离线** —— 不联网、不上传、不涉及账号
* **图形界面**用系统原生窗口 + Windows 材质（Aero）

> **免责声明**：本项目仅做**本地格式转换**，用于转换你自己合法获得的文件。
> 它不绕过任何账号或付费校验，也不包含任何音频/歌词素材。
> 使用前请阅读 [NOTICE.md](NOTICE.md) 中的授权范围与风险说明。
>
> *Disclaimer: format conversion only, for files you obtained legally. It bypasses no
> account or payment check and ships no audio or lyrics. See [NOTICE.md](NOTICE.md).*

功能一览 / At a glance:

| 中文 | English |
| --- | --- |
| 批量多线程转换，进度与速度实时显示 | Batch, multi-threaded, with live progress and speed |
| 按语言 / 按歌手 / 语言+歌手 两级的目录整理，文件夹名可自定义 | Organise by language / artist / both, folder names editable |
| 「预览落点」先看每首歌放哪 | Preview where every track will land |
| 保留标签、歌词、封面，可导出 `.lrc` | Keeps tags, lyrics, cover art; optional `.lrc` sidecar |
| 删除原文件前逐字节校验 | Byte-for-byte verification before deleting a source |
| 磨砂玻璃界面、原生窗口、检查更新 | Frosted glass, native window, update check |

---

## 安装

```bash
# 只要命令行（零依赖）
pip install .

# 命令行 + numpy 加速（可选，XOR 步骤从 ~7 MB/s 提到 >200 MB/s，回退路径按字节一致）
pip install ".[fast]"

# 图形界面 / 打包 EXE
pip install ".[gui,build]"
```

从源码直接运行（无需安装）：

```bash
# 图形界面
python src/ncmdump_gui.py

# 命令行（不安装也不设环境变量，用包的 -m 入口）
python -c "import sys; sys.path.insert(0,'src'); from ncmdump.ncm2mp3 import main; sys.exit(main())" ./some-dir
```

> 注意：源码树里**没有** `ncmdump/` 顶层目录，包在 `src/ncmdump/`。
> 因此不安装时必须让 `src` 进入 `sys.path`（如上），否则 `python -m ncmdump.ncm2mp3` 会找不到包。

## 命令行用法

### 输出目录怎么分（4 种方式）

```bash
# 1) 不分类，全部丢进输出目录
ncmdump ./in -o D:/Music --organize-by none

# 2) 按语言分目录，语言 -> 文件夹名完全自定义
ncmdump ./in -o D:/Music \
  --organize "ja=J-Pop,zh=中文,ko=K-Pop,ru=Русский,other=其他"

# 3) 按歌手分目录
ncmdump ./in -o D:/Music --organize-by artist

# 4) 语言 + 歌手 两级：D:/Music/J-Pop/宇多田ヒカル/…
ncmdump ./in -o D:/Music \
  --organize "ja=J-Pop,zh=中文,ko=K-Pop,ru=Русский,other=其他" --artist-folders
```

`--plan` 可以先看落点再决定（不转换，且用**真实路由逻辑**计算，不会与实际结果不一致）：

```bash
ncmdump ./in -o D:/Music --organize "ja=J-Pop,other=其他" --plan
```

识别的语种与代号（`--organize` 的键）：

| 代号 | 判据 | 界面默认文件夹名 |
| --- | --- | --- |
| `ja` | 含假名（元数据或歌词） | 日文 |
| `zh` | 含汉字且无假名 | 中文 |
| `ko` | 含谚文 | 韩文 |
| `ru` | 含西里尔字母 | 俄文 |
| `other` | 其余（英法等拉丁字母无法区分） | 其他 |

> 纯汉字标题（如 `万華鏡`）单看字形与中文无从区分，因此归到 `zh`；
> 只有歌词里出现假名时才会判为 `ja`。文件夹名会被自动清洗（去掉 `\ / : * ? " < > |`）。

| 参数 | 说明 |
| --- | --- |
| `paths` | 文件、目录或通配符；默认当前目录（递归） |
| `-o, --out` | 输出目录，默认与源文件同目录 |
| `-j, --jobs` | 并行数，默认 min(CPU 核数, 8) |
| `--organize MAP` | `语言代号=文件夹名`，逗号分隔；未列出的语种落到 `other` |
| `--organize-by` | `language`（默认）/ `artist` / `none` |
| `--artist-folders` | 语言目录下再按歌手分一层 |
| `--plan` | 预览每首歌落点 |
| `--keep` | 保留 `.ncm`（**默认转换成功后删除**） |
| `--no-verify` | 删除前跳过逐字节校验（更快，不推荐） |
| `--no-cover` / `--no-lyrics` | 不下载封面 / 不处理歌词 |
| `--plain-lrc` | 歌词去掉 `[mm:ss]` 时间轴 |
| `--meta` / `--dry-run` / `--no-recursive` | 列元数据 / 列文件 / 不递归 |

**删除原文件的安全性**：默认先把"解密结果"与"落盘文件"的音频区**逐字节流式比对**，
只有完全一致才删除源文件；不一致会报错并保留原文件。

**并发安全**：多进程/多线程同时转换时，暂存文件名含进程号与线程号，最终文件名用
`O_CREAT|O_EXCL` **原子认领**（重名自动变成 `名字 (2)` 等）。早期版本用"先检查再创建"
且暂存名只由歌名决定，**同名的不同歌曲并发转换会互相覆盖、丢文件**（已有回归测试守着）。

## 图形界面

```bash
python src/ncmdump_gui.py
```

把 `.ncm` 文件或文件夹拖进去 → 选输出目录（**留空则输出到源文件所在目录**）→ 开始转换。

界面右下角显示 **`v1.0.0 · Calvin Vollerei Studio`**，旁边是「**检查更新**」按钮：
它会请求 GitHub Releases 并对比版本，显示「有新版本 vX.Y.Z」（带链接）、「已是最新版本」，
或「检查失败（可手动查看 Releases）」——**任何网络/仓库问题都不会报错或卡住界面**。
发布流程见 [PUBLISHING.md](PUBLISHING.md)。

**目录整理**（界面里可全部自定义）：

| 界面控件 | 作用 |
| --- | --- |
| 「整理方式」下拉框 | 不分类 / 按语言 / 按歌手 / 按语言 + 歌手（两级） |
| 语言文件夹名输入框 | 每个语种一个输入框（`zh` `ja` `ko` `ru` `other`），**名字随你改**：`中文`、`J-Pop`、`VIP(Japanness)` 都行 |
| 「预览落点」按钮 | 不转换，先列出每首歌会落到哪个文件夹、各多少首 |

* 使用**系统默认窗口**：标题栏、边框拉伸、Aero Snap、双击最大化、Alt+Space 全部由 Windows 提供
* 窗口材质通过 [pywinstyles](https://github.com/Akascape/py-window-styles) 驱动，**构建时固定为 Aero**
  （只有一种材质，界面上不显示材质标签、也没有切换按钮，因此没有多余的样式代码要打包）；
  遮色层很薄，透明度较高
* 转换跑在线程池，进度条 / 当前阶段 / 实时速度 / 逐文件日志实时刷新
* 「停止」只中断后续任务，正在处理的文件会跑完，不会留下半个文件

想换成别的材质，两种办法（都不需要改界面）：

```bash
# 运行时看效果
NCM_GLASS=acrylic python src/ncmdump_gui.py   # acrylic | mica | aero | solid
```

```python
# 或者改一行再构建
src/ncmdump_gui.py:  DEFAULT_MATERIAL = "aero"   →  "acrylic" / "mica" / "solid"
```

想更透明/更实，改同文件里的 `GLASS_TINT_ALPHA`（默认 46，越小越透）。

### 启动性能

导入阶段做过一轮优化，实测（Windows / Python 3.11，到窗口构造完成）：

| 项目 | 优化前 | 优化后 |
| --- | --- | --- |
| `import ncmdump.ncm_core` | 104 ms | **7 ms** |
| `import ncmdump.ncm2mp3` | 33 ms | **1 ms** |
| 到窗口可用总计 | 369 ms | **196 ms** |

做法：numpy 改成**首次使用时才探测**（原来在模块顶层 import，代价 ~100 ms，
而 CLI 可能一次都不用）；CLI 的线程池 / 多进程 import 移进函数内部
（GUI 不走 CLI，却要替它付导入成本）。CI 有一条测试守着"导入不得拉起 numpy"。

### 打包 EXE

PyInstaller 会在执行 spec 前 `chdir` 到 **spec 所在目录**，所以入口脚本与 spec 放在一起（都在 `src/`）：

```bash
cd src
python -m PyInstaller --noconfirm --clean --distpath ../dist --workpath ../build ncm_gui.spec
../dist/NCMConverter/NCMConverter.exe --selftest --report=selftest.txt
```

产物是 **onedir**（约 95 MB，201 个文件）：比 onefile 启动快得多，且不会每次运行自我解压。
`numpy` 不打包（可选依赖，会给产物加 ~23 MB OpenBLAS）。

## 作为库使用

```python
from ncmdump import decrypt_file

r = decrypt_file("song.ncm")
print(r.fmt, r.meta.title, r.meta.artist_line, len(r.audio))
open("song." + r.fmt, "wb").write(r.audio)
```

大文件用流式接口，内存占用与文件大小无关：

```python
from ncmdump.ncm_core import decrypt_to_file

info = decrypt_to_file("song.ncm", "song.flac")
print(info.payload_len)   # 纯音频帧字节数
print(info.audio_len)     # 写出总字节数（含 FLAC 元数据块）
```

> 自己比对文件时请比 `payload_len` 那一段：`audio_len` 含约 8 KB 的 FLAC 元数据区。

## 算法说明

### 容器结构

```
0x00   8   "CTENFDAM"
0x08   2   填充
0x0a   4   密钥长度
0x0e   n   密钥数据   每字节 ^0x64 → AES-128-ECB(CORE_KEY) → 丢前 17 字节
  ..   4   元数据长度 m
  ..   m   元数据     每字节 ^0x63 → 丢前 22 字节 → base64 → AES-128-ECB(META_KEY)
  ..   4   四个 0x00
  ..   1   0x01
  ..   4   封面块长度
  ..       exactly 那么多字节的封面（无封面时是 4 字节占位）
  ..       加密音频：每字节 ^= keybox[i & 0xFF]，直到文件结束
```

### 三个容易踩的坑

1. **keybox 不是标准 RC4 输出。** KSA 之后还有一次映射：

   ```js
   r.map(function(e,t,r){ t = t+1 & 255; var i = r[t], n = r[t+i & 255]; return r[i+n & 255] })
   ```

   少这一步解出来全是噪声。

2. **音频起点不是"紧跟长度字段"。** 音频从 `元数据结束 + 9 + max(封面块长度, 4)` 开始。
   少跳或多跳 4 字节会让密钥流错位：产物仍"看起来像 FLAC"，但每个采样字节都是移位的。

3. **那个长度字段不是音频长度**，而是封面块长度。音频从该位置**一直到文件尾**，
   比这个数字大得多（实测样例：字段 610722，音频 65 MB）。只读声明的长度会得到一个
   几百 KB 的坏文件。

### 正确性验证

* AES 用 FIPS-197 附录 C 向量自检：`python src/ncmdump/aes_lite.py`
* 容器往返测试用**合成容器**（`tests/ncm_builder.py` 自行构造合法 `.ncm`），
  因此**仓库内不含任何受版权保护的素材**
* 开发过程中曾与参考实现的输出做过逐字节（SHA-256）对拍

## 测试

```bash
pip install ".[dev]"
pytest -q
ruff check src tests
```

CI（GitHub Actions）在 Windows / Ubuntu 上跑 3.9–3.12 的测试，并额外做两项守卫：
**零依赖导入检查**（只用标准库跑核心）与**机密扫描**（禁止提交音频、凭据、绝对路径）。

## 仓库结构

```
src/ncmdump/            核心包（纯标准库）
  aes_lite.py           AES-128/192/256-ECB + PKCS#7（含 FIPS 自检）
  ncm_core.py           容器解析、密钥盒、元数据、流式解密
  tags.py               FLAC(Vorbis comment/PICTURE) 与 MP3(ID3v2.3) 标签写入
  ncm2mp3.py            批量命令行
src/ncmdump_gui.py      图形界面
src/ncm_gui.spec        PyInstaller 配置
tests/                  单元测试（含合成 .ncm 构造器）
```

## 许可证与合规

[MIT](LICENSE)。算法来源、两个十六进制常量的性质、授权范围与风险提示详见 [NOTICE.md](NOTICE.md)。
安全相关的问题请按 [SECURITY.md](SECURITY.md) 反馈。
