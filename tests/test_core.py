# -*- coding: utf-8 -*-
"""Container parsing and decryption, exercised against synthetic containers."""

import os
import struct

import ncm_builder as B
import pytest

from ncmdump.ncm_core import (
    NcmError,
    decrypt_file,
    decrypt_stream,
    decrypt_to_file,
    sniff_format,
    xor_keystream,
)

AUDIO = B.fake_flac(b"payload-" * 500)


@pytest.fixture()
def ncm(tmp_path):
    def make(name="song.ncm", **kwargs):
        path = tmp_path / name
        B.write(str(path), kwargs.pop("audio", AUDIO), **kwargs)
        return str(path)

    return make


def test_roundtrip_without_cover(ncm):
    result = decrypt_file(ncm())
    assert result.audio == AUDIO
    assert result.fmt == "flac"
    assert result.mime == "audio/flac"


def test_roundtrip_with_cover(ncm):
    # the cover block changes the audio offset, which is the classic off-by-four trap
    path = ncm(cover=b"\xff\xd8\xff" + b"C" * 1234)
    result = decrypt_file(path)
    assert result.audio == AUDIO
    assert result.cover_len == len(b"\xff\xd8\xff" + b"C" * 1234)


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 255, 256, 257, 4096, 4097])
def test_audio_lengths_roundtrip(ncm, size):
    audio = B.fake_flac(bytes(size))
    path = ncm(audio=audio)
    assert decrypt_file(path).audio == audio


def test_metadata_is_decoded(ncm):
    path = ncm(title="日本語のうた", artists=["ロクデナシ", "Guest"], album="アルバム")
    result = decrypt_file(path)
    assert result.meta.title == "日本語のうた"
    assert result.meta.artists == ["ロクデナシ", "Guest"]
    assert result.meta.album == "アルバム"
    assert result.meta.artist_line == "ロクデナシ; Guest"


def test_dj_variant_uses_main_music(ncm):
    path = ncm(prefix="dj")
    # the builder only writes the music shape, so this documents the fallback path
    assert decrypt_file(path).meta.title == "Test Song"


def test_metadata_only_mode_reports_no_audio(ncm):
    result = decrypt_file(ncm(), read_audio=False)
    assert result.audio == b""
    assert result.fmt == "flac"
    assert result.audio_len == 0          # the header only knows the cover length


def test_streaming_matches_in_memory(ncm, tmp_path):
    path = ncm()
    in_memory = decrypt_file(path).audio
    out = tmp_path / "out.flac"
    info = decrypt_to_file(path, str(out))
    assert out.read_bytes() == in_memory
    assert info.audio_len == len(in_memory)
    assert info.payload_len < info.audio_len      # metadata region is not audio


def test_payload_len_matches_flac_block_walk(ncm, tmp_path):
    """payload_len is the audio frames only: file size minus the FLAC metadata blocks."""
    path = ncm()
    out = tmp_path / "out.flac"
    info = decrypt_to_file(path, str(out))
    blob = out.read_bytes()
    pos = 4
    while True:
        head = blob[pos:pos + 4]
        last = bool(head[0] & 0x80)
        pos += 4 + int.from_bytes(head[1:4], "big")
        if last:
            break
    assert info.payload_len == len(blob) - pos
    # AUDIO is the whole FLAC stream; payload_len must be smaller by the metadata region
    assert info.payload_len < len(AUDIO)
    assert info.audio_len == len(blob)
    assert len(AUDIO) - info.payload_len == pos


def test_bad_magic_is_rejected(tmp_path):
    path = tmp_path / "bad.ncm"
    path.write_bytes(b"NOTANNCM" + b"\x00" * 64)
    with pytest.raises(NcmError):
        decrypt_file(str(path))


def test_truncated_header_is_rejected(tmp_path):
    path = tmp_path / "trunc.ncm"
    path.write_bytes(b"CTENFDAM\x01\x70" + b"\x00" * 4)
    with pytest.raises(NcmError):
        decrypt_file(str(path))


def test_truncated_after_metadata_yields_no_audio(tmp_path):
    blob = B.build(AUDIO)
    path = tmp_path / "cut.ncm"
    path.write_bytes(blob[:200])          # cut inside the header area
    with pytest.raises(NcmError):
        decrypt_to_file(str(path), str(tmp_path / "out.bin"))


def test_empty_key_material_is_rejected(tmp_path):
    blob = bytearray(B.build(AUDIO))
    blob[10:14] = struct.pack("<I", 0)     # key length 0
    path = tmp_path / "nokey.ncm"
    path.write_bytes(bytes(blob))
    with pytest.raises(NcmError):
        decrypt_file(str(path))


def test_decrypt_stream_from_file_object(ncm):
    with open(ncm(), "rb") as fh:
        assert decrypt_stream(fh).audio == AUDIO


@pytest.mark.parametrize("head,expected", [
    (b"fLaC", "flac"),
    (b"ID3\x04", "mp3"),
    (b"\xff\xfb\x90\x00", "mp3"),
    (b"OggS", "ogg"),
    (b"RIFF\x00\x00\x00\x00WAVE", "wav"),
    (b"\x00\x00\x00\x18ftypM4A ", "m4a"),
    (b"MAC ", "ape"),
    (b"FRM8", "dff"),
    (b"nope", ""),
])
def test_sniff_format(head, expected):
    assert sniff_format(head) == expected


def test_sniff_format_short_input():
    assert sniff_format(b"") == ""
    assert sniff_format(b"fL") == ""


# --- the XOR primitive, both fast paths --------------------------------------

@pytest.mark.parametrize("size", [0, 1, 255, 256, 257, 1000, 65537])
@pytest.mark.parametrize("phase", [0, 1, 255])
def test_xor_keystream_all_phases(size, phase):
    keybox = bytes((i * 7 + 3) & 0xFF for i in range(256))
    data = bytes((i * 13) & 0xFF for i in range(size))
    want = bytes(b ^ keybox[(phase + i) & 0xFF] for i, b in enumerate(data))
    assert xor_keystream(data, keybox, phase) == want


def test_xor_keystream_falls_back_without_numpy(monkeypatch):
    """The pure-Python path must agree with the accelerated one."""
    from ncmdump import ncm_core

    keybox = bytes(range(256))
    data = os.urandom(5000)
    fast = ncm_core.xor_keystream(data, keybox, 7)
    monkeypatch.setattr(ncm_core, "_np", None)
    slow = ncm_core.xor_keystream(data, keybox, 7)
    assert slow == fast


def test_xor_keystream_empty():
    assert xor_keystream(b"", bytes(range(256))) == b""
