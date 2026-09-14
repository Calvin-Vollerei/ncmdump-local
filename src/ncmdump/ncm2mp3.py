# -*- coding: utf-8 -*-
"""批量把网易云 `.ncm` 转成本地可播放的音频（纯 Python，零第三方依赖）。

用法示例::

    python ncm2mp3.py                      # 转换当前目录及子目录里所有 .ncm
    python ncm2mp3.py "VipSongsDownload"   # 指定目录
    python ncm2mp3.py a.ncm b.ncm          # 指定文件
    python ncm2mp3.py -o D:\\out -j 8      # 指定输出目录 + 8 进程
    python ncm2mp3.py --meta               # 只看元数据，不解密
    python ncm2mp3.py --no-cover           # 不写入封面

输出与源文件同名（用内嵌的歌名），保留原有 `.lrc` 歌词。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import threading
import time

# `concurrent.futures` pulls in threading and multiprocessing, which costs tens of
# milliseconds to import — and the GUI never runs the CLI, it calls _convert directly. The
# pool classes are therefore imported inside the functions that actually need them.

if __package__ in (None, ""):  # 允许 `python ncmdump/ncm2mp3.py` 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ncmdump.ncm_core import NcmError, decrypt_file, decrypt_to_file
    from ncmdump.tags import TagInfo, artist_folder, guess_language, write_tags
else:
    from .ncm_core import NcmError, decrypt_file, decrypt_to_file
    from .tags import TagInfo, artist_folder, guess_language, write_tags

ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize(name: str, fallback: str = "unnamed") -> str:
    """Turn a song title into something Windows will accept as a file name."""
    cleaned = ILLEGAL.sub("_", (name or "").strip())
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        cleaned = fallback
    # keep well clear of MAX_PATH surprises
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip(" .")
    return cleaned


def find_inputs(paths, recursive: bool = True):
    """Expand the CLI arguments into a sorted list of .ncm files."""
    found = []
    for item in paths or ["."]:
        if os.path.isdir(item):
            if recursive:
                for root, _dirs, files in os.walk(item):
                    for name in files:
                        if name.lower().endswith(".ncm"):
                            found.append(os.path.join(root, name))
            else:
                for name in os.listdir(item):
                    full = os.path.join(item, name)
                    if os.path.isfile(full) and name.lower().endswith(".ncm"):
                        found.append(full)
        elif os.path.isfile(item):
            found.append(item)
        else:
            # glob-ish pattern
            import glob

            found.extend(glob.glob(item, recursive=recursive))
    unique = []
    seen = set()
    for path in found:
        key = os.path.abspath(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return sorted(unique)


def load_lyrics(ncm_path: str) -> str:
    """Read the sidecar .lrc next to an .ncm file (if any)."""
    base = os.path.splitext(ncm_path)[0]
    for ext in (".lrc", ".LRC"):
        candidate = base + ext
        if os.path.isfile(candidate):
            for encoding in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
                try:
                    with open(candidate, "r", encoding=encoding) as fh:
                        return fh.read().strip()
                except (UnicodeDecodeError, UnicodeError):
                    continue
    return ""


def split_lrc(lyrics: str):
    """Split an LRC into (plain text, LRC with a JSON header stripped).

    NetEase writes a JSON line such as ``{"t":0,"c":[{"tx":"作词: "},...]}`` at the top
    of newer .lrc files; ETS2 and most players only understand the ``[mm:ss.xx]`` lines.
    """
    lines = lyrics.splitlines()
    while lines and lines[0].lstrip().startswith("{"):
        lines.pop(0)
    lrc = "\n".join(lines).strip()
    plain = []
    for line in lines:
        text = re.sub(r"^(\[[^\]]*\])+", "", line).strip()
        if text:
            plain.append(text)
    return "\n".join(plain), lrc


def _organize_options(lang_map):
    """Split a lang_map into (mode, artist_folders, {code: folder}).

    The reserved ``__mode__`` / ``__artist__`` keys travel with the mapping so the extra
    options reach the worker through the existing job tuple instead of widening it.
    """
    if not lang_map:
        return "none", False, {}
    mapping = dict(lang_map)
    mode = mapping.pop("__mode__", "language")
    artist = bool(mapping.pop("__artist__", False))
    # tolerate the historical shape where only {"ja": .., "other": ..} was passed
    if mode not in ("language", "artist", "none"):
        mode = "language"
    return mode, artist, mapping


def convert_one(job):
    """Worker: decrypt one file, write tags + lyrics. Returns a result dict."""
    (src, out_dir, keep, want_cover, want_lyrics, lrc_plain, lang_map, organize, verify) = job
    return _convert(src, out_dir, keep, want_cover, want_lyrics, lrc_plain,
                    lang_map, organize, verify)


def _convert(src, out_dir, keep, want_cover, want_lyrics, lrc_plain,
             lang_map, organize, verify, progress=None):
    """The actual conversion. `progress(stage, fraction)` is called as work proceeds so a
    GUI can show movement during a single (possibly 200 MB) file."""
    organize_by, artist_folders, folders = _organize_options(lang_map)
    organize = organize and organize_by != "none"
    lang_map = folders
    name = os.path.basename(src)
    base = os.path.splitext(name)[0]
    tmp_path = None
    claimed = None
    started = time.time()
    result = {"src": src, "name": name, "ok": False, "error": "", "out": "",
              "bytes": 0, "fmt": "", "title": "", "artist": "", "lang": "", "notes": []}
    report = progress or (lambda stage, fraction=0.0: None)
    try:
        # 1) parse the header only (cheap): we need the real title before naming the file
        report("metadata")
        parsed = decrypt_file(src, read_audio=False)
        title = parsed.meta.title or base
        result["title"] = parsed.meta.title
        result["artist"] = parsed.meta.artist_line

        out_name = sanitize(title, base)
        # lyrics are read up front: they are the more reliable language signal when the
        # title is romanised, and --organize keys off the language
        lyrics = load_lyrics(src)
        target_dir = _target_dir(src, out_dir, parsed, lang_map, organize,
                                 organize_by, artist_folders)
        result["lang"] = target_dir
        os.makedirs(target_dir, exist_ok=True)

        # The staging name must be unique per worker: two *different* songs can share a
        # title, and a title-derived name would have them writing the same temp file.
        staging_name = ".%s.%d.%d.ncmtmp" % (out_name, os.getpid(), threading.get_ident())
        tmp_path = os.path.join(target_dir, staging_name)
        report("decrypt", 0.0)
        info = decrypt_to_file(src, tmp_path)
        report("decrypt", 1.0)
        fmt = info.fmt or "bin"

        cover = None
        cover_mime = ""
        if want_cover:
            report("cover")
            cover, cover_mime = _fetch_cover(info.meta.album_pic)
            if info.meta.album_pic and not cover:
                result["notes"].append("cover-failed")

        plain, lrc = split_lrc(lyrics) if lyrics else ("", "")

        report("tag")
        # Tag the staged file *before* it is published: until this point the final name is
        # untouched, so a failure here leaves nothing behind but the temp file.
        tag = TagInfo(
            title=info.meta.title or base,
            artists=info.meta.artists,
            album=info.meta.album,
            cover=cover,
            cover_mime=cover_mime,
            lyrics=plain,
        )
        status = write_tags(tmp_path, "flac" if fmt == "flac" else fmt, tag)
        if status not in ("tagged", "no-tags"):
            result["notes"].append("tags:" + status)

        # Now claim the final name atomically and publish in one step.
        claimed = _claim_path(os.path.join(target_dir, out_name + "." + fmt))
        out_path = claimed
        _move_into_place(tmp_path, out_path, keep)
        tmp_path = None
        stem = os.path.splitext(out_path)[0]

        # sidecar lyrics file
        if lyrics and want_lyrics:
            with open(stem + ".lrc", "w", encoding="utf-8") as fh:
                fh.write((plain if lrc_plain else lrc) + "\n")

        # only now, with the audio and tags both written, is it safe to drop the source
        if not keep:
            report("verify")
            if verify and not _verify_output(src, out_path, fmt):
                result["error"] = "校验失败，已保留原文件（输出: %s）" % out_path
                return result
            try:
                os.remove(src)
                sidecar = os.path.splitext(src)[0] + ".lrc"
                if os.path.isfile(sidecar) and want_lyrics:
                    os.remove(sidecar)
            except OSError:
                result["notes"].append("unlink-failed")

        result.update(ok=True, out=out_path, bytes=info.payload_len or info.audio_len, fmt=fmt)
    except NcmError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # never let one bad file kill the batch
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        # if we reserved a final name but never published, release the placeholder so the
        # directory is not littered with zero-byte files
        if claimed and not result["ok"]:
            _release_claim(claimed)
        result["seconds"] = round(time.time() - started, 2)
    return result


def _claim_path(target: str) -> str:
    """Atomically reserve a final output path, returning the name actually claimed.

    A placeholder is created with O_CREAT|O_EXCL, which is the only check-then-create a
    second thread cannot win. Without this, two conversions of different songs that happen
    to share a title would pick the same output name and overwrite each other.
    """
    stem, ext = os.path.splitext(target)
    index = 0
    while True:
        candidate = target if index == 0 else "%s (%d)%s" % (stem, index, ext)
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            index += 1
            if index > 9999:                       # pathological; give up loudly
                raise
            continue
        os.close(fd)
        return candidate


def _release_claim(claimed: str) -> None:
    """Drop our placeholder if we never managed to publish over it."""
    try:
        os.remove(claimed)
    except OSError:
        pass


def _move_into_place(tmp_path: str, out_path: str, keep: bool) -> None:
    """Publish the finished file, replacing our own placeholder atomically.

    Falls back to copy+delete across volumes. An already-existing file is not an error:
    that is our own reservation from ``_claim_path``.
    """
    try:
        os.replace(tmp_path, out_path)
        return
    except OSError:
        pass
    shutil.copyfile(tmp_path, out_path)
    os.remove(tmp_path)


def _target_dir(src, out_dir, parsed, lang_map, organize,
                organize_by="language", artist_folders=False):
    """Pick the destination folder for one track.

    Two independent levels can be combined:

    * ``organize_by`` — ``"language"`` groups by detected script, ``"artist"`` by the first
      credited artist, ``"none"`` keeps everything flat. ``lang_map`` maps a language code
      to a folder name (fully user-editable).
    * ``artist_folders`` — when grouping by language this adds a per-artist sub-folder, so
      ``language/artist/`` falls out of the same settings.

    Language comes from the metadata *and* the sidecar lyrics: a Japanese track with a
    romanised title carries no kana in its tags, and would otherwise land in "other".
    """
    parts = []
    if organize and lang_map is not None:
        if organize_by == "artist":
            sub = sanitize(artist_folder(parsed.meta.artists), "")
            if sub:
                parts.append(sub)
        elif organize_by == "language":
            lyrics = load_lyrics(src)
            lang = guess_language(parsed.meta.title, parsed.meta.artists, parsed.meta.album,
                                  lyrics)
            sub = lang_map.get(lang) or lang_map.get("other") or ""
            if sub:
                parts.append(sanitize(sub, "Other"))
            if artist_folders:
                sub = sanitize(artist_folder(parsed.meta.artists), "")
                if sub:
                    parts.append(sub)

    base = out_dir or os.path.dirname(os.path.abspath(src))
    return os.path.join(base, *parts) if parts else base


def _flac_or_id3_audio_offset(blob: bytes, fmt: str) -> int:
    """Offset where the audio frames start (skipping metadata blocks / ID3 tag)."""
    if fmt == "flac":
        if blob[:4] != b"fLaC":
            return 0
        pos = 4
        while pos + 4 <= len(blob):
            last = bool(blob[pos] & 0x80)
            size = int.from_bytes(blob[pos + 1:pos + 4], "big")
            pos += 4 + size
            if last:
                break
        return pos
    if fmt == "mp3" and blob[:3] == b"ID3":
        size = 0
        for b in blob[6:10]:
            size = (size << 7) | (b & 0x7F)
        return 10 + size
    return 0


def _verify_output(src: str, out_path: str, fmt: str) -> bool:
    """Confirm the written file carries exactly the audio the source decrypts to.

    Runs before the original is deleted, so a bad write can never cost the user a file.
    Both sides are streamed with their metadata regions stripped, in 1 MB chunks.
    """
    import tempfile

    from ncmdump.ncm_core import decrypt_to_file as _dec

    tmp = None
    want_stream = None
    got_stream = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".chk")
        os.close(fd)
        info = _dec(src, tmp)
        if not info.payload_len:
            return False
        want_stream = _open_audio_stream(tmp, info.fmt)
        got_stream = _open_audio_stream(out_path, fmt)
        compared = 0
        while True:
            expected = next(want_stream, b"")
            got = next(got_stream, b"")
            if got != expected:
                if os.environ.get("NCM_DEBUG_VERIFY"):
                    print("[verify] %s: mismatch at %d (want %d, got %d bytes)"
                          % (os.path.basename(out_path), compared, len(expected), len(got)),
                          file=sys.stderr)
                return False
            compared += len(expected)
            if not expected:
                return True
    except Exception:
        return False
    finally:
        for stream in (want_stream, got_stream):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _open_audio_stream(path: str, fmt: str):
    """Yield the audio bytes of `path` with metadata stripped (streaming)."""
    fh = open(path, "rb")
    head = fh.read(4)
    start = 0
    if fmt == "flac" and head == b"fLaC":
        start = 4
        while True:
            block = fh.read(4)
            if len(block) < 4:
                break
            last = bool(block[0] & 0x80)
            size = int.from_bytes(block[1:4], "big")
            fh.seek(size, 1)
            start += 4 + size
            if last:
                break
    elif fmt == "mp3":
        fh.seek(0)
        if fh.read(3) == b"ID3":
            fh.seek(6)
            raw = fh.read(4)
            if len(raw) == 4:
                size = 0
                for b in raw:
                    size = (size << 7) | (b & 0x7F)
                start = 10 + size
    fh.seek(start)
    try:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                return
            yield chunk
    finally:
        fh.close()


def _move_into_place(tmp_path: str, out_path: str, keep: bool) -> None:
    """Publish the finished file. Falls back to copy+delete across volumes."""
    try:
        os.replace(tmp_path, out_path)
        return
    except OSError:
        pass
    shutil.copyfile(tmp_path, out_path)
    os.remove(tmp_path)


def _fetch_cover(url: str, timeout: int = 20, limit: int = 12 << 20):
    """Download cover art. Failure is never fatal.

    The read is capped: an unbounded ``resp.read()`` on a hostile or broken CDN response
    would happily pull gigabytes into memory (and the cover ends up embedded in the file
    anyway, where a sane one is a few hundred KB).
    """
    if not url:
        return None, ""
    try:
        import ssl
        import urllib.request

        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://music.163.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            return None, ""            # implausibly large: treat as a failed download
        if not data:
            return None, ""
        mime = "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        return data, mime
    except Exception:
        return None, ""


def _make_pool(jobs: int, payload):
    """Prefer real processes (the XOR plus file I/O dominates), fall back to threads.

    Restricted environments (some sandboxes) deny the named pipes multiprocessing needs,
    and the failure surfaces while the lazy `map` is being consumed — so the fallback has
    to wrap iteration, not just construction.
    """
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

    def iter_with_fallback():
        try:
            executor = ProcessPoolExecutor(max_workers=jobs)
        except (OSError, PermissionError, ImportError):
            executor = None
        if executor is not None:
            try:
                for item in executor.map(convert_one, payload):
                    yield item
                executor.shutdown(wait=True)
                return
            except (OSError, PermissionError) as exc:
                # only "access denied on the IPC pipe" justifies degrading to threads
                if getattr(exc, "winerror", None) != 5 and not isinstance(exc, PermissionError):
                    executor.shutdown(wait=True)
                    raise
                executor.shutdown(wait=True)
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for item in pool.map(convert_one, payload):
                yield item

    return None, iter_with_fallback()


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f%s" % (size, unit)
        size /= 1024
    return "%.1fGB" % size


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ncm2mp3",
        description="批量解密网易云 .ncm 为 FLAC/MP3（本地、离线、零依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("paths", nargs="*", help="文件、目录或通配符（默认当前目录）")
    parser.add_argument("-o", "--out", help="输出目录（默认与源文件同目录）")
    parser.add_argument("-j", "--jobs", type=int, default=0,
                        help="并行进程数（默认：CPU 核心数，最多 8）")
    parser.add_argument("--no-cover", action="store_true", help="不写入/不下载封面")
    parser.add_argument("--no-lyrics", action="store_true", help="不复制歌词")
    parser.add_argument("--plain-lrc", action="store_true",
                        help="歌词去掉 [mm:ss] 时间轴（给不支持时间轴的播放器）")
    parser.add_argument("--meta", action="store_true", help="只打印元数据，不解密")
    parser.add_argument("--keep", action="store_true", help="保留原始 .ncm 文件")
    parser.add_argument("--no-verify", action="store_true",
                        help="删除原文件前不做事后逐字节校验（更快，但不推荐）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将要处理的文件")
    parser.add_argument("--plan", action="store_true",
                        help="只打印每首歌将落到哪个目录（配合 --organize 使用），不转换")
    parser.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    parser.add_argument("--organize", metavar="MAP",
                        help="语言 -> 目录名，如 \"ja=J-Pop,zh=中文,ko=K-Pop,ru=Русский,other=其他\"；"
                             "未列出的语言落到 other 指定的目录（默认：不按语言分目录）")
    parser.add_argument("--organize-by", choices=["language", "artist", "none"],
                        default="language",
                        help="分目录依据：language=按语种；artist=按歌手；none=不分（默认 language）")
    parser.add_argument("--artist-folders", action="store_true",
                        help="在语言目录下再按歌手分一层（language/artist/…）")
    args = parser.parse_args(argv)

    lang_map = None
    if args.organize or args.organize_by == "artist":
        lang_map = {"__mode__": args.organize_by,
                    "__artist__": bool(args.artist_folders)}
        if args.organize:
            for part in args.organize.split(","):
                if not part.strip():
                    continue
                key, _, value = part.partition("=")
                lang_map[key.strip().lower()] = value.strip()
        if args.organize_by == "language" and not lang_map.get("other"):
            print("--organize 必须为 other 指定一个目录，例如 other=Others")
            return 2

    files = find_inputs(args.paths, recursive=not args.no_recursive)
    if not files:
        print("没有找到 .ncm 文件。")
        return 2

    print("找到 %d 个 .ncm 文件，合计 %s" % (files.__len__(), human(sum(os.path.getsize(f) for f in files))))
    if args.dry_run:
        for path in files:
            print("  " + path)
        return 0

    if args.meta or args.plan:
        failures = 0
        buckets = {}
        plan_by, plan_artist, plan_map = _organize_options(lang_map)
        for path in files:
            try:
                info = decrypt_file(path, read_audio=False)
                if args.plan and args.organize_by != "none":
                    # reuse the real routing so the preview cannot drift from the run
                    target = _target_dir(path, args.out or "", info, plan_map,
                                         True, plan_by, plan_artist)
                    buckets.setdefault(target or "(源目录)", []).append(
                        "%s - %s" % (info.meta.artist_line or "?", info.meta.title or "?"))
                    print("%-30s -> %s" % (info.meta.title[:30] or os.path.basename(path),
                                           target or "(源目录)"))
                else:
                    print("%-46s %-5s %s - %s" % (
                        os.path.basename(path)[:46], info.fmt or "?",
                        info.meta.artist_line or "?", info.meta.title or "?"))
            except Exception as exc:
                failures += 1
                print("%-46s 失败: %s" % (os.path.basename(path)[:46], exc))
        if args.plan and buckets:
            print("\n汇总：")
            for target, items in sorted(buckets.items()):
                print("  %-40s %d 首" % (os.path.relpath(target, args.out) if args.out
                                         and target.startswith(args.out) else target,
                                         len(items)))
        return 1 if failures else 0

    jobs = args.jobs or min(8, (os.cpu_count() or 4))
    payload = [(path, args.out, args.keep, not args.no_cover,
                not args.no_lyrics, args.plain_lrc, lang_map,
                lang_map is not None and args.organize_by != "none",
                not args.no_verify)
               for path in files]

    started = time.time()
    done = ok = failed = 0
    total_bytes = 0
    errors = []

    if jobs <= 1:
        iterator = (convert_one(job) for job in payload)
        executor = None
    else:
        executor, iterator = _make_pool(jobs, payload)

    try:
        for result in iterator:
            done += 1
            if result["ok"]:
                ok += 1
                total_bytes += result["bytes"]
                note = ("  [" + ",".join(result["notes"]) + "]") if result["notes"] else ""
                print("[%d/%d] %s -> %s  %s  %.1fs%s" % (
                    done, len(files), result["name"][:40],
                    os.path.basename(result["out"]), human(result["bytes"]),
                    result.get("seconds", 0), note))
            else:
                failed += 1
                errors.append((result["name"], result["error"]))
                print("[%d/%d] %s  失败: %s" % (done, len(files), result["name"][:40], result["error"]))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130

    elapsed = time.time() - started
    print("\n完成：成功 %d，失败 %d，用时 %.1fs（%s/s）" % (
        ok, failed, elapsed, human(total_bytes / elapsed) if elapsed else "-"))
    if errors:
        print("失败清单：")
        for name, err in errors[:20]:
            print("  %s : %s" % (name, err))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
