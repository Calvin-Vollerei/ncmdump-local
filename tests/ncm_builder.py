# -*- coding: utf-8 -*-
"""Build synthetic (but byte-accurate) .ncm containers for the tests.

Why synthetic: the project must never ship real .ncm files, and they carry copyrighted
audio anyway. Building the container ourselves also pins the layout — if the parser and
this builder ever disagree, the round-trip tests fail, which is exactly the signal we want.

Layout, matching what the reference implementation reads (see ncm_core._parse_header):

    "CTENFDAM" | 2 pad | u32 key_len | key blob
    u32 meta_len | metadata blob
    "00 00 00 00" | 0x01 | u32 cover_len | cover bytes | audio
"""
from __future__ import annotations

import base64
import json
import struct

from ncmdump.aes_lite import AES
from ncmdump.ncm_core import CORE_KEY, META_KEY

# a fixed key-material blob; length only matters, not the value
KEYDATA = (b"104091397427940E7fT49x7dof9OKCgg9cdvhEuezy3iZCL1nFvBFd1T4uSktAJKmwZXsijPbijliion"
           b"VUXXg9plTbXEclAE9Lb")


def _aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB encryption with PKCS#7 padding.

    Only AES-128: that is all the container format needs (both the key blob and the
    metadata blob use a 16-byte key), and it keeps this helper small. Correctness is
    pinned by ``test_aes.py::test_builder_encryptor_matches_openssl`` against a vector
    produced by OpenSSL.
    """
    if len(key) != 16:
        raise ValueError("the fixture builder only implements AES-128")
    from ncmdump.aes_lite import SBOX

    aes = AES(key)
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    out = bytearray()
    for offset in range(0, len(data), 16):
        out += _encrypt_block(aes, list(data[offset:offset + 16]), SBOX)
    return bytes(out)


def _encrypt_block(aes: AES, block: list, sbox: list) -> bytes:
    """Minimal AES-128 encrypt: the inverse of aes_lite's decryption."""
    rk = aes._round_keys
    state = [block[i] ^ rk[0][i] for i in range(16)]

    def shift_rows(s):
        return [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
                s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]

    def mix_columns(s):
        def xt(a):
            return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF

        out = [0] * 16
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c:4 * c + 4]
            out[4 * c + 0] = xt(a0) ^ (xt(a1) ^ a1) ^ a2 ^ a3
            out[4 * c + 1] = a0 ^ xt(a1) ^ (xt(a2) ^ a2) ^ a3
            out[4 * c + 2] = a0 ^ a1 ^ xt(a2) ^ (xt(a3) ^ a3)
            out[4 * c + 3] = (xt(a0) ^ a0) ^ a1 ^ a2 ^ xt(a3)
        return out

    for rnd in range(1, aes.nr):
        state = [sbox[b] for b in state]
        state = shift_rows(state)
        state = mix_columns(state)
        state = [state[i] ^ rk[rnd][i] for i in range(16)]
    state = [sbox[b] for b in state]
    state = shift_rows(state)
    return bytes(state[i] ^ rk[aes.nr][i] for i in range(16))


def keystream(keydata: bytes, length: int) -> bytes:
    """Reproduce the player's keystream (RC4 KSA + the extra shuffle step)."""
    n = len(keydata)
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (box[i] + j + keydata[i % n]) & 0xFF
        box[i], box[j] = box[j], box[i]
    shuffled = [0] * 256
    for t in range(256):
        t1 = (t + 1) & 0xFF
        i1 = box[t1]
        n1 = box[(t1 + i1) & 0xFF]
        shuffled[t] = box[(i1 + n1) & 0xFF]
    return bytes(shuffled[i % 256] for i in range(length))


def fake_flac(payload: bytes = b"audio-bytes-here") -> bytes:
    """A byte string that sniffs as FLAC AND is shaped like a real stream.

    Two details matter because the payload-length helper walks the metadata blocks:

    * the metadata region must be a properly terminated STREAMINFO block;
    * the audio must start with a real frame-sync word. A FLAC block header is
      ``<last-flag><type:7> <size:24>``, so a body beginning with 0xFF would look like a
      "last" block plus a huge length — the parser would then consume the audio as
      metadata. ``0xFF 0xF9`` is a valid 16-bit frame sync (fixed blocksize) and keeps the
      byte after it from being read as a block header.
    """
    streaminfo = bytes(34)                       # 34 bytes is the real STREAMINFO size
    block = bytes([0x80 | 0]) + len(streaminfo).to_bytes(3, "big") + streaminfo
    return b"fLaC" + block + b"\xff\xf9" + payload


def build(audio: bytes, title: str = "Test Song", artists=("Artist A",),
          album: str = "Album", cover: bytes = b"", keydata: bytes = KEYDATA,
          fmt: str = "flac", prefix: str = "music") -> bytes:
    """Assemble a complete container around `audio`."""
    key_plain = b"neteasecloudmusic" + keydata
    key_blob = _aes_ecb_encrypt(CORE_KEY, key_plain)
    key_enc = bytes(b ^ 0x64 for b in key_blob)

    meta = {
        "musicId": "1", "musicName": title,
        "artist": [[a, "1"] for a in artists],
        "albumId": "1", "album": album,
        "albumPic": "", "bitrate": 999000, "duration": 1000, "format": fmt,
    }
    payload = (prefix + ":").encode("ascii") + json.dumps(meta, ensure_ascii=False).encode("utf-8")
    meta_blob = b"163 key(Don't modify):" + base64.b64encode(_aes_ecb_encrypt(META_KEY, payload))
    meta_enc = bytes(b ^ 0x63 for b in meta_blob)

    ks = keystream(keydata, len(audio))
    audio_enc = bytes(a ^ b for a, b in zip(audio, ks))

    # no cover still reserves 4 bytes, which is why the parser uses max(cover_len, 4)
    block = cover if cover else b"\x00\x00\x00\x00"
    if len(block) < 4:
        block = block + b"\x00" * (4 - len(block))

    out = bytearray()
    out += b"CTENFDAM\x01\x70"
    out += struct.pack("<I", len(key_enc)) + key_enc
    out += struct.pack("<I", len(meta_enc)) + meta_enc
    out += b"\x00\x00\x00\x00" + b"\x01"
    out += struct.pack("<I", len(block)) + block
    out += audio_enc
    return bytes(out)


def write(path, audio: bytes, **kwargs):
    data = build(audio, **kwargs)
    with open(path, "wb") as fh:
        fh.write(data)
    return str(path)
