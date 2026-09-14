# -*- coding: utf-8 -*-
"""Write metadata into the decrypted audio, with **no third-party dependencies**.

Supported:
  * FLAC  -> Vorbis comment block + PICTURE block (cover art), written in place, size
             differences absorbed by a PADDING block so the audio never moves.
  * MP3   -> ID3v2.3 tag (TIT2/TPE1/TALB/TRCK/APIC/USLT), replacing any existing tag.

Anything else is passed through untouched — the audio is what matters.
"""
from __future__ import annotations

import os
import struct
from typing import List, Optional, Sequence, Tuple

try:  # Python 3.9+
    from typing import TypedDict
except ImportError:  # pragma: no cover
    TypedDict = dict  # type: ignore

__all__ = ["TagInfo", "write_tags", "read_flac_tags", "guess_language"]

# FLAC block types
_FLAC_STREAMINFO = 0
_FLAC_PADDING = 1
_FLAC_VORBIS_COMMENT = 4
_FLAC_PICTURE = 6


class TagInfo(dict):
    """Simple container: title / artists / album / track / cover (bytes) / cover_mime / lyrics."""

    def __init__(
        self,
        title: str = "",
        artists: Optional[Sequence[str]] = None,
        album: str = "",
        track: str = "",
        cover: Optional[bytes] = None,
        cover_mime: str = "",
        lyrics: str = "",
    ):
        super().__init__(
            title=title or "",
            artists=list(artists or []),
            album=album or "",
            track=track or "",
            cover=cover,
            cover_mime=cover_mime or "",
            lyrics=lyrics or "",
        )

    @property
    def title(self) -> str:
        return self["title"]

    @property
    def artists(self) -> List[str]:
        return self["artists"]

    @property
    def album(self) -> str:
        return self["album"]

    @property
    def track(self) -> str:
        return self["track"]

    @property
    def cover(self) -> Optional[bytes]:
        return self["cover"]

    @property
    def cover_mime(self) -> str:
        return self["cover_mime"]

    @property
    def lyrics(self) -> str:
        return self["lyrics"]

    @property
    def artist_line(self) -> str:
        return "; ".join(self["artists"])

    def is_empty(self) -> bool:
        return not any([self["title"], self["artists"], self["album"], self["track"],
                        self["cover"], self["lyrics"]])


def write_tags(path: str, fmt: str, tag: TagInfo) -> str:
    """Apply tags to the file at ``path``. Returns a short status string."""
    if tag.is_empty():
        return "no-tags"
    if fmt == "flac":
        return _write_flac(path, tag)
    if fmt == "mp3":
        return _write_id3(path, tag)
    return "unsupported-format"


def guess_language(title: str, artists: Optional[Sequence[str]] = None,
                   album: str = "", lyrics: str = "") -> str:
    """Rough language guess: ``"ja"``, ``"zh"`` or ``"other"``.

    Order matters here, and it is deliberate:

    1. kana anywhere in the metadata  -> Japanese (kanji alone is ambiguous, kana is not);
    2. kana in the lyrics             -> Japanese. This comes *before* the Chinese test
       because a kanji-only title such as ``万華鏡`` is indistinguishable from Chinese by
       script alone, while Japanese lyrics reliably contain kana;
    3. any other CJK in the metadata  -> Chinese;
    4. otherwise                      -> other.
    """
    import re

    kana = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
    cjk = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
    texts = [title or "", album or ""] + list(artists or [])
    if any(kana.search(t) for t in texts):
        return "ja"
    if lyrics and kana.search(lyrics):
        return "ja"
    if any(cjk.search(t) for t in texts):
        return "zh"
    return "other"


# ---------------------------------------------------------------- FLAC -------

def _vorbis_comment_block(comments: List[Tuple[str, str]]) -> bytes:
    vendor = b"ncmdump-local"
    out = bytearray()
    out += struct.pack("<I", len(vendor)) + vendor
    real = [(k, v) for k, v in comments if v]
    out += struct.pack("<I", len(real))
    for key, value in real:
        item = ("%s=%s" % (key, value)).encode("utf-8")
        out += struct.pack("<I", len(item)) + item
    return bytes(out)


def _picture_block(cover: bytes, mime: str, width: int = 0, height: int = 0, depth: int = 0) -> bytes:
    desc = b""
    out = bytearray()
    out += struct.pack(">I", 3)                      # picture type 3 = front cover
    out += struct.pack(">I", len(mime)) + mime.encode("ascii", "replace")
    out += struct.pack(">I", len(desc)) + desc
    out += struct.pack(">I", width)
    out += struct.pack(">I", height)
    out += struct.pack(">I", depth)
    out += struct.pack(">I", 0)                      # 0 = "unspecified" colour count
    out += struct.pack(">I", len(cover)) + cover
    return bytes(out)


def _flac_block(kind: int, last: bool, payload: bytes) -> bytes:
    return bytes([(0x80 if last else 0x00) | (kind & 0x7F)]) + \
        len(payload).to_bytes(3, "big") + payload


def read_flac_tags(path: str) -> List[Tuple[str, str]]:
    """Read Vorbis comments from a FLAC file (used by --verify and tests)."""
    with open(path, "rb") as fh:
        if fh.read(4) != b"fLaC":
            raise ValueError("not a FLAC file")
        while True:
            head = fh.read(4)
            if len(head) < 4:
                return []
            last, kind = head[0] & 0x80, head[0] & 0x7F
            size = int.from_bytes(head[1:4], "big")
            data = fh.read(size)
            if kind == _FLAC_VORBIS_COMMENT:
                return _parse_vorbis(data)
            if last:
                return []


def _parse_vorbis(data: bytes) -> List[Tuple[str, str]]:
    pos = 0
    (vlen,) = struct.unpack_from("<I", data, pos)
    pos += 4 + vlen
    (count,) = struct.unpack_from("<I", data, pos)
    pos += 4
    out = []
    for _ in range(count):
        (n,) = struct.unpack_from("<I", data, pos)
        pos += 4
        item = data[pos:pos + n].decode("utf-8", "replace")
        pos += n
        key, _, value = item.partition("=")
        out.append((key.upper(), value))
    return out


def _write_flac(path: str, tag: TagInfo) -> str:
    with open(path, "rb") as fh:
        blob = fh.read()
    if blob[:4] != b"fLaC":
        return "not-flac"

    pos = 4
    blocks: List[Tuple[int, bytes]] = []
    while pos + 4 <= len(blob):
        head = blob[pos:pos + 4]
        last, kind = bool(head[0] & 0x80), head[0] & 0x7F
        size = int.from_bytes(head[1:4], "big")
        blocks.append((kind, blob[pos + 4:pos + 4 + size]))
        pos += 4 + size
        if last:
            break
    # `pos` is where the audio starts; the rewrite helpers stream from there rather than
    # slicing the payload out, so no copy is taken here
    if not blocks or blocks[0][0] != _FLAC_STREAMINFO:
        return "not-flac"

    comments: List[Tuple[str, str]] = [("TITLE", tag.title)]
    if tag.album:
        comments.append(("ALBUM", tag.album))
    for artist in tag.artists:
        comments.append(("ARTIST", artist))
    if tag.track:
        comments.append(("TRACKNUMBER", tag.track))
    if tag.artists:
        comments.append(("ALBUMARTIST", tag.artists[0]))
    if tag.lyrics:
        comments.append(("LYRICS", tag.lyrics))

    want: List[Tuple[int, bytes]] = [(_FLAC_VORBIS_COMMENT, _vorbis_comment_block(comments))]
    if tag.cover:
        mime = tag.cover_mime or _sniff_mime(tag.cover)
        width, height = _image_size(tag.cover)
        want.append((_FLAC_PICTURE, _picture_block(tag.cover, mime, width, height)))

    replace_kinds = {k for k, _ in want}
    kept = [(k, p) for k, p in blocks if k not in replace_kinds]

    def encode(items: List[Tuple[int, bytes]], pad_extra: int = 0) -> bytes:
        chunks = []
        for idx, (kind, payload) in enumerate(items):
            is_last = (idx == len(items) - 1) and pad_extra == 0
            chunks.append(_flac_block(kind, is_last, payload))
        if pad_extra > 0:
            chunks.append(_flac_block(_FLAC_PADDING, True, b"\x00" * pad_extra))
        return b"".join(chunks)

    old_meta_len = pos
    new_blocks = list(kept) + list(want)
    body = encode(new_blocks)

    if (len(body) + 4) > old_meta_len:
        # Try to reclaim space from an existing PADDING block first.
        padded = _absorb_with_padding(new_blocks, (len(body) + 4) - old_meta_len)
        if padded is not None:
            body = encode(padded)
    elif (len(body) + 4) < old_meta_len:
        # We have room to spare: record it as padding so a later re-run can reuse it.
        body = encode(new_blocks, pad_extra=old_meta_len - (len(body) + 4))

    if (len(body) + 4) == old_meta_len:
        # Same size: overwrite the metadata region in place, audio stays untouched.
        with open(path, "r+b") as fh:
            fh.seek(4)
            fh.write(body)
    else:
        # Larger: relocate the audio (streamed) after the new metadata region.
        _rewrite_flac_shifting_audio(path, body, old_meta_len)
    return "tagged"


def _rewrite_flac_shifting_audio(path: str, body: bytes, old_meta_len: int) -> None:
    """Rewrite a FLAC file with a larger metadata region, preserving the audio bytes.

    Streams through a temporary file so a 200 MB track does not have to be held twice
    in memory.
    """
    tmp = path + ".ncmtmp"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        src.seek(old_meta_len)
        dst.write(b"fLaC")
        dst.write(body)
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            dst.write(chunk)
    os.replace(tmp, path)


def _absorb_with_padding(blocks: List[Tuple[int, bytes]], need: int) -> Optional[List[Tuple[int, bytes]]]:
    """Remove `need` bytes by consuming an existing PADDING block (if large enough)."""
    for idx, (kind, payload) in enumerate(blocks):
        if kind == _FLAC_PADDING and len(payload) >= need + 4:
            rest = len(payload) - need
            out = list(blocks)
            if rest > 4:
                out[idx] = (_FLAC_PADDING, b"\x00" * (rest - 4))
            else:
                del out[idx]
            return out
    return None


# ----------------------------------------------------------------- MP3 -------

def _synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _text_frame(frame_id: str, text: str) -> bytes:
    if not text:
        return b""
    # encoding 1 = UTF-16 with BOM (widest compatibility for CJK)
    payload = b"\x01\xff\xfe" + text.encode("utf-16-le")
    return frame_id.encode("ascii") + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _apic_frame(cover: bytes, mime: str) -> bytes:
    payload = b"\x00" + mime.encode("ascii", "replace") + b"\x00" + b"\x03" + b"\x00" + cover
    return b"APIC" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _uslt_frame(lyrics: str) -> bytes:
    if not lyrics:
        return b""
    payload = b"\x01eng\xff\xfe" + lyrics.encode("utf-16-le")
    return b"USLT" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _write_id3(path: str, tag: TagInfo) -> str:
    """Write an ID3v2.3 tag in front of an MP3.

    Streams the audio into a sibling temp file and swaps it in, instead of reading the
    whole file, truncating the original and rewriting it. The old shape could leave a
    partial file if the process died mid-write, and held roughly 2.3x the file size in
    RAM; the FLAC path already worked this way.
    """
    skip = 0
    with open(path, "rb") as fh:
        head = fh.read(10)
        if head[:3] == b"ID3":        # drop an existing tag so re-runs do not stack up
            size = 0
            for b in head[6:10]:
                size = (size << 7) | (b & 0x7F)
            skip = 10 + size

    frames = b"".join([
        _text_frame("TIT2", tag.title),
        _text_frame("TPE1", tag.artist_line),
        _text_frame("TALB", tag.album),
        _text_frame("TRCK", tag.track),
        _uslt_frame(tag.lyrics),
        _apic_frame(tag.cover, tag.cover_mime or _sniff_mime(tag.cover)) if tag.cover else b"",
    ])
    header = b"ID3\x03\x00\x00" + _synchsafe(len(frames))

    tmp = "%s.id3tmp" % path
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(header)
            dst.write(frames)
            src.seek(skip)
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return "tagged"


# ------------------------------------------------------------- helpers -------

def _sniff_mime(data: Optional[bytes]) -> str:
    if not data:
        return "application/octet-stream"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _image_size(data: bytes) -> Tuple[int, int]:
    """Best-effort width/height for JPEG and PNG (0,0 when unknown)."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", data[i + 5:i + 9])
                    return width, height
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                (seg,) = struct.unpack(">H", data[i + 2:i + 4])
                i += 2 + seg
    except Exception:
        pass
    return 0, 0
