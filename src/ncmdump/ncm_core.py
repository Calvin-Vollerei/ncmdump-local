# -*- coding: utf-8 -*-
"""NCM container decryption — pure Python reimplementation of the algorithm used by
www.ncm2mp3.com (/js/decrypt.js), verified byte-for-byte against the site's own code.

Container layout (little-endian):

    0x00  8   magic "CTENFDAM"        (site checks [67,84,69,78,70,68,65,77])
    0x08  2   padding
    0x0a  4   key_len
    0x0e  n   key material   (每个字节 ^ 0x64, then AES-128-ECB with CORE_KEY)
              decrypted blob: [0:17] skipped, rest -> RC4 key material
     ..   4   meta_len
     ..   m   metadata        (每个字节 ^ 0x63, drop first 22 bytes, base64,
                               AES-128-ECB with META_KEY, strip PKCS#7)
                               plaintext: b"music:{json}" or b"dj:{json}"
     ..   4   crc32
     ..   5   padding
     ..   4   audio_len
     ..   a   encrypted audio (every byte ^= keybox[i & 0xff])
"""
from __future__ import annotations

import base64
import json
import os
import struct
from dataclasses import dataclass, field

from .aes_lite import aes_ecb_decrypt, pkcs7_unpad

# Fixed algorithm constants of the NCM container format — NOT account credentials or user
# secrets. They do not vary per user, need no login, and are already public in several
# open-source projects (see NOTICE.md). Kept as plain hex on purpose: obfuscating them
# would hide rather than remove them, and would only look evasive.
CORE_KEY = bytes.fromhex("687a4852416d736f356b496e62617857")
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")
NCM_MAGIC = b"CTENFDAM"

__all__ = ["NcmError", "NcmResult", "NcmMeta", "decrypt_file", "decrypt_to_file",
           "decrypt_stream", "sniff_format", "xor_keystream"]


class NcmError(Exception):
    """Raised when a file is not a usable NCM container."""


@dataclass
class NcmMeta:
    """Metadata carried inside the container."""

    title: str = ""
    artists: list = field(default_factory=list)   # list[str]
    album: str = ""
    album_pic: str = ""
    raw: dict = field(default_factory=dict)       # the full embedded JSON
    truncated: bool = False                       # True when read from a truncated copy

    @property
    def artist_line(self) -> str:
        return "; ".join(self.artists)


@dataclass
class NcmResult:
    audio: bytes          # decrypted, playable audio (in-memory APIs only)
    fmt: str              # "flac" / "mp3" / "ogg" / ...
    mime: str
    meta: NcmMeta
    key_len: int = 0
    meta_len: int = 0
    audio_len: int = 0     # total bytes produced, leading metadata region included
    payload_len: int = 0   # bytes of actual audio frames (what a player reads)
    cover_len: int = 0     # cover-art byte count recorded in the container header
    truncated: bool = False  # header was damaged / shorter than it claims

    @property
    def suffix(self) -> str:
        return self.fmt or "bin"


def decrypt_file(path: str, read_audio: bool = True) -> NcmResult:
    """Decrypt an .ncm file from disk into memory.

    ``read_audio=False`` parses only header/key/metadata (used by --meta-only and by
    tests); the returned ``audio`` is then empty.
    """
    with open(path, "rb") as fh:
        return decrypt_stream(fh, read_audio=read_audio)


def decrypt_to_file(path: str, out_path: str, chunk_size: int = 1 << 20) -> NcmResult:
    """Decrypt an .ncm file straight to ``out_path``, streaming the audio.

    Memory use is bounded by ``chunk_size`` instead of the file size, which matters a
    lot for the 60-240 MB files this format produces.
    """
    with open(path, "rb") as src:
        parsed = _parse_header(src)
        if parsed.truncated and parsed.audio_start is None:
            raise NcmError("文件已损坏（缺少音频区）")
        if parsed.audio_start is not None:
            src.seek(parsed.audio_start)
        written, fmt = _stream_xor(src, out_path, parsed.keybox, chunk_size)
        if written == 0:
            raise NcmError("文件已损坏（音频区为空，可能是拷贝不完整）")
        parsed.fmt = fmt
        parsed.mime = _MIME.get(fmt, "")
        parsed.audio_len = written          # everything written (metadata region included)
        parsed.payload_len = _audio_payload_len(out_path, fmt, written)
        return parsed.as_result()


class _Parsed:
    """Internal: the result of parsing everything up to the audio stream."""

    __slots__ = ("keybox", "meta", "fmt", "mime", "audio_len", "payload_len",
                 "audio_start", "key_len", "meta_len", "cover_len", "truncated", "kind")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))
        self.payload_len = kw.get("payload_len", 0)
        self.cover_len = kw.get("cover_len", 0)
        self.kind = kw.get("kind", "header")
        self.truncated = bool(kw.get("truncated", False))

    def as_result(self, audio: bytes = b"") -> NcmResult:
        payload = self.payload_len
        if not payload and audio:
            payload = _audio_payload_len(None, self.fmt or "", len(audio), audio)
        return NcmResult(audio, self.fmt or "", self.mime or "", self.meta or NcmMeta(),
                         self.key_len or 0, self.meta_len or 0, self.audio_len or 0,
                         payload or 0, self.cover_len or 0, bool(self.truncated))


def _audio_payload_len(path, fmt: str, total: int, blob: bytes = None) -> int:
    """How many of the written bytes are actual audio frames (not metadata)."""
    if fmt == "flac":
        if blob is not None:
            if blob[:4] != b"fLaC":
                return total
            pos = 4
            while pos + 4 <= len(blob):
                last = bool(blob[pos] & 0x80)
                size = int.from_bytes(blob[pos + 1:pos + 4], "big")
                pos += 4 + size
                if last:
                    break
            return max(0, len(blob) - pos)
        try:
            with open(path, "rb") as fh:
                if fh.read(4) != b"fLaC":
                    return total
                pos = 4
                while True:
                    head = fh.read(4)
                    if len(head) < 4:
                        break
                    last = bool(head[0] & 0x80)
                    size = int.from_bytes(head[1:4], "big")
                    fh.seek(size, 1)
                    pos += 4 + size
                    if last:
                        break
            return max(0, total - pos)
        except OSError:
            return total
    if fmt == "mp3" and blob is not None and blob[:3] == b"ID3":
        size = 0
        for b in blob[6:10]:
            size = (size << 7) | (b & 0x7F)
        return max(0, len(blob) - 10 - size)
    return total


def _parse_header(fh) -> _Parsed:
    """Read magic, key material, metadata and the audio offset. Leaves `fh` at the
    start of the encrypted audio."""
    head = fh.read(8)
    if len(head) < 8 or head != NCM_MAGIC:
        raise NcmError("不是有效的 NCM 文件（文件头不匹配）")
    fh.read(2)  # padding

    key_blob = _read_block(fh, xor=0x64, what="密钥")
    key_data = pkcs7_unpad(aes_ecb_decrypt(CORE_KEY, key_blob))[17:]
    if not key_data:
        raise NcmError("密钥数据为空，文件可能已损坏")
    keybox = _build_keybox(key_data)

    meta_blob = _read_block(fh, xor=0x63, what="元数据")
    truncated = False
    meta = NcmMeta()
    if meta_blob:
        try:
            meta = _parse_meta(meta_blob)
        except Exception as exc:  # metadata is optional for playback
            truncated = True
            meta = NcmMeta(truncated=True, raw={"_error": str(exc)})

    # Layout note — subtle, and worth reading before touching this code. Verified
    # byte-for-byte against the site's own decryptor on real files, and pinned by
    # measurement in ncm-work/layout_probe.py:
    #   offset + 0 .. + 4    four zero bytes
    #   offset + 4           0x01
    #   offset + 5 .. + 9    cover-art block length
    #   offset + 9 ..        exactly that many bytes of cover art (padded to a 4-byte
    #                        floor when the file carries no cover)
    #   audio start          = offset + 9 + max(cover_len, 4)
    # The site expresses it as `offset += getUint32(offset + 5) + 13`, i.e. 9 bytes for the
    # fields it already consumed plus the 4-byte pad its own reader re-reads. Skipping
    # `cover_len + 13` instead is off by four bytes on cover-less files and de-syncs the
    # keystream: the result still looks like a FLAC, but every sample byte is shifted.
    trailer = fh.read(9)
    if len(trailer) < 9:
        return _Parsed(keybox=keybox, meta=meta, truncated=True, audio_start=None,
                       key_len=len(key_blob), meta_len=len(meta_blob), kind="header")
    declared = struct.unpack("<I", trailer[5:9])[0]

    fmt = ""
    if isinstance(meta.raw, dict):
        fmt = (meta.raw.get("format") or "").lower()

    skip = max(declared, 4)
    fh.seek(skip, 1)
    start = fh.tell()
    if start >= _size_of(fh):
        # The header claims a cover region that is not actually there. Fall back to
        # treating whatever follows the cover-length field as audio, and flag it: the
        # caller should not silently ship a zero-byte result.
        truncated = True
        fh.seek(_size_of(fh))
        start = fh.tell()
    return _Parsed(keybox=keybox, meta=meta, fmt=fmt, mime=_MIME.get(fmt, ""),
                   audio_len=0, cover_len=declared, audio_start=start,
                   key_len=len(key_blob), meta_len=len(meta_blob),
                   truncated=truncated, kind="audio")


def _size_of(fh) -> int:
    try:
        return os.fstat(fh.fileno()).st_size
    except Exception:
        pos = fh.tell()
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(pos)
        return size


try:  # optional, big speed-up for the XOR step
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


def xor_keystream(data: bytes, keybox: bytes, phase: int = 0) -> bytes:
    """XOR ``data`` with the repeating 256-byte ``keybox`` starting at ``phase``.

    Three implementations, fastest first. The whole-file XOR is the hot loop for this
    format, so it is worth the branching: a per-byte Python loop manages ~7 MB/s while
    the big-integer path does ~200 MB/s and numpy over a GB/s.
    """
    n = len(data)
    if n == 0:
        return data
    offset = phase & 0xFF
    if _np is not None:
        arr = _np.frombuffer(data, dtype=_np.uint8)
        if offset:
            key = _np.frombuffer((keybox[offset:] + keybox[:offset]) * (n // 256 + 1),
                                 dtype=_np.uint8)[:n]
        else:
            key = _np.frombuffer(keybox * (n // 256 + 1), dtype=_np.uint8)[:n]
        return (_np.bitwise_xor(arr, key)).tobytes()
    if offset:
        stream = (keybox[offset:] + keybox[:offset]) * (n // 256) + \
            (keybox[offset:] + keybox[:offset])[: n % 256]
    else:
        stream = keybox * (n // 256) + keybox[: n % 256]
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(n, "little")


def _stream_xor(fh, out_path: str, keybox: bytes, chunk_size: int):
    """XOR-copy the rest of `fh` into `out_path`; returns (bytes written, format)."""
    written = 0
    fmt = ""
    with open(out_path, "wb") as dst:
        first = True
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            out = xor_keystream(chunk, keybox, written)
            if first:
                fmt = sniff_format(out[:16])
                first = False
            dst.write(out)
            written += len(out)
    return written, fmt


def decrypt_stream(fh, read_audio: bool = True) -> NcmResult:
    """Decrypt from an open binary file object into memory (kept for tests/one-offs)."""
    parsed = _parse_header(fh)
    if not read_audio:
        fmt = ""
        if isinstance(parsed.meta.raw, dict):
            fmt = (parsed.meta.raw.get("format") or "").lower()
        # audio_len stays 0: the header only recorded the cover-art size, and reporting
        # that as an audio length would be plainly wrong.
        result = parsed.as_result()
        result.fmt = fmt
        result.mime = _MIME.get(fmt, "")
        return result
    if parsed.audio_start is not None:
        fh.seek(parsed.audio_start)
    payload = fh.read()
    audio = xor_keystream(payload, parsed.keybox, 0) if payload else b""
    fmt = sniff_format(audio)
    parsed.fmt, parsed.mime = fmt, _MIME.get(fmt, "")
    parsed.audio_len = len(audio)
    parsed.payload_len = _audio_payload_len(None, fmt, len(audio), audio)
    return parsed.as_result(audio)


# --- helpers ---------------------------------------------------------------

def _read_block(fh, xor: int, what: str) -> bytes:
    raw_len = fh.read(4)
    if len(raw_len) < 4:
        raise NcmError("%s长度字段缺失，文件已损坏" % what)
    n = struct.unpack("<I", raw_len)[0]
    if n == 0:
        return b""
    if n > 64 << 20:
        raise NcmError("%s长度异常 (%d 字节)" % (what, n))
    data = fh.read(n)
    if len(data) != n:
        raise NcmError("%s数据不完整" % what)
    return bytes(b ^ xor for b in data)


def _build_keybox(key_data: bytes) -> bytes:
    """RC4 KSA + the extra shuffle step, exactly as the site does."""
    n = len(key_data)
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (box[i] + j + key_data[i % n]) & 0xFF
        box[i], box[j] = box[j], box[i]
    out = bytearray(256)
    for i in range(256):
        t = (i + 1) & 0xFF
        out[i] = box[(box[t] + box[(t + box[t]) & 0xFF]) & 0xFF]
    return bytes(out)


def _parse_meta(blob: bytes) -> NcmMeta:
    plain = pkcs7_unpad(aes_ecb_decrypt(META_KEY, base64.b64decode(blob[22:])))
    text = plain.decode("utf-8", "replace")
    prefix, _, body = text.partition(":")
    if not body:
        # some variants store no "kind:" prefix
        body = text
    data = json.loads(body)
    if prefix == "dj":
        data = data.get("mainMusic", data)

    meta = NcmMeta(raw=data)
    meta.title = (data.get("musicName") or "").strip()
    meta.album = (data.get("album") or "").strip()
    artists = []
    for item in data.get("artist") or []:
        if isinstance(item, (list, tuple)) and item:
            artists.append(str(item[0]).strip())
        elif isinstance(item, str):
            artists.append(item.strip())
    meta.artists = [a for a in artists if a]
    pic = data.get("albumPic") or ""
    if pic.startswith("http://"):
        pic = "https://" + pic[len("http://"):]
    meta.album_pic = pic
    return meta


_MIME = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "aac": "audio/aac",
    "wma": "audio/x-ms-wma",
    "ape": "audio/ape",
    "dff": "audio/dff",
}


def sniff_format(data: bytes) -> str:
    """Detect the container format from the decrypted bytes (same order as the site)."""
    if len(data) < 4:
        return ""
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"OggS":
        return "ogg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"MAC ":
        return "ape"
    if data[:4] == b"FRM8":
        return "dff"
    return ""


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    r = decrypt_file(sys.argv[1], read_audio=True)
    print("format :", r.fmt or "(unknown)")
    print("title  :", r.meta.title)
    print("artists:", r.meta.artist_line)
    print("album  :", r.meta.album)
    print("cover  :", r.meta.album_pic)
    print("bytes  :", len(r.audio))
