# ncmdump-local

把网易云音乐的 `.ncm` 文件**在本地**转换成可播放的 FLAC/MP3，支持图形界面与命令行。

* **核心零第三方依赖** —— 纯 Python 标准库实现 AES、容器解析、FLAC/ID3 标签写入
* **全程离线** —— 不联网、不上传、不涉及账号
* **图形界面**用系统原生窗口 + Windows 材质（Acrylic/Mica/Aero）

> **免责声明**：本项目仅做**本地格式转换**，用于转换你自己合法获得的文件。
> 它不绕过任何账号或付费校验，也不包含任何音频/歌词素材。
> 使用前请阅读 [NOTICE.md](NOTICE.md) 中的授权范围与风险说明。

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

```bash
# 转换目录里全部 .ncm（默认保留原文件）
ncmdump ./某目录

# 按语言分目录 + 8 并行 + 不写封面
ncmdump ./某目录 -o D:/Music -j 8 --no-cover \
  --organize "ja=VIP(Japanness),other=VIP(Other language)"

# 只看元数据 / 预览落点，都不转换
ncmdump ./某目录 --meta
ncmdump ./某目录 --plan -o D:/Music --organize "ja=A,other=B"
```

| 参数 | 说明 |
| --- | --- |
| `paths` | 文件、目录或通配符；默认当前目录（递归） |
| `-o, --out` | 输出目录，默认与源文件同目录 |
| `-j, --jobs` | 并行数，默认 min(CPU 核数, 8) |
| `--organize MAP` | 按语言分目录，如 `ja=J,other=O` |
| `--plan` | 预览每首歌落点 |
| `--keep` | 保留 `.ncm`（**默认转换成功后删除**） |
| `--no-verify` | 删除前跳过逐字节校验（更快，不推荐） |
| `--no-cover` / `--no-lyrics` | 不下载封面 / 不处理歌词 |
| `--plain-lrc` | 歌词去掉 `[mm:ss]` 时间轴 |
| `--meta` / `--dry-run` / `--no-recursive` | 列元数据 / 列文件 / 不递归 |

**删除原文件的安全性**：默认先把"解密结果"与"落盘文件"的音频区**逐字节流式比对**，
只有完全一致才删除源文件；不一致会报错并保留原文件。

## 图形界面

```bash
python src/ncmdump_gui.py
```

把 `.ncm` 文件或文件夹拖进去 → 选输出目录（**留空则输出到源文件所在目录**）→ 开始转换。

* 使用**系统默认窗口**：标题栏、边框拉伸、Aero Snap、双击最大化、Alt+Space 全部由 Windows 提供
* 窗口材质通过 [pywinstyles](https://github.com/Akascape/py-window-styles) 驱动；
  点 `◐` 在 **Acrylic → Mica → Aero → 纯色** 之间切换
* 转换跑在线程池，进度条 / 当前阶段 / 实时速度 / 逐文件日志实时刷新
* 「停止」只中断后续任务，正在处理的文件会跑完，不会留下半个文件

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
