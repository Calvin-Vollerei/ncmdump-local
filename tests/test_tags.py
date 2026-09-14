# -*- coding: utf-8 -*-
"""Tag writing and language detection — no third-party libraries involved."""

import re

import pytest

from ncmdump.tags import TagInfo, _sniff_mime, guess_language, read_flac_tags, write_tags

FLAC_AUDIO = b"\xff\xf8" + bytes(range(200))


def _flac_bytes(tmp_path, name="t.flac"):
    """Write a tiny real FLAC header (STREAMINFO only) plus fake frames."""
    streaminfo = bytes(34)                      # 34 zero bytes is a valid-size block
    block = bytes([0x80 | 0]) + len(streaminfo).to_bytes(3, "big") + streaminfo
    path = tmp_path / name
    path.write_bytes(b"fLaC" + block + FLAC_AUDIO)
    return str(path)


def _audio_of(blob: bytes) -> bytes:
    pos = 4
    while True:
        head = blob[pos:pos + 4]
        last = bool(head[0] & 0x80)
        pos += 4 + int.from_bytes(head[1:4], "big")
        if last:
            break
    return blob[pos:]


def test_writes_vorbis_comments(tmp_path):
    path = _flac_bytes(tmp_path)
    status = write_tags(path, "flac",
                        TagInfo(title="歌名", artists=["A", "B"], album="专辑"))
    assert status == "tagged"
    tags = {}
    for key, value in read_flac_tags(path):
        tags.setdefault(key, []).append(value)
    assert tags["TITLE"] == ["歌名"]
    assert tags["ARTIST"] == ["A", "B"]
    assert tags["ALBUM"] == ["专辑"]
    assert tags["ALBUMARTIST"] == ["A"]


def test_audio_survives_tagging(tmp_path):
    path = _flac_bytes(tmp_path)
    before = _audio_of(open(path, "rb").read())
    write_tags(path, "flac", TagInfo(title="x", cover=b"\xff\xd8\xff" + b"C" * 4000))
    after = _audio_of(open(path, "rb").read())
    assert after == before                    # audio must move but never change


def test_tagging_is_idempotent(tmp_path):
    path = _flac_bytes(tmp_path)
    tag = TagInfo(title="same", artists=["A"], album="B")
    write_tags(path, "flac", tag)
    first = open(path, "rb").read()
    write_tags(path, "flac", tag)
    second = open(path, "rb").read()
    assert _audio_of(second) == _audio_of(first)
    assert len(read_flac_tags(path)) == len([1 for k, _ in read_flac_tags(path)])
    # re-running must not stack duplicate comment blocks
    assert second.count(b"TITLE=same") == 1


def test_cover_is_embedded_as_picture_block(tmp_path):
    path = _flac_bytes(tmp_path)
    write_tags(path, "flac", TagInfo(title="t", cover=b"\xff\xd8\xff" + b"C" * 100))
    blob = open(path, "rb").read()
    pos, kinds = 4, []
    while True:
        head = blob[pos:pos + 4]
        last = bool(head[0] & 0x80)
        kinds.append(head[0] & 0x7F)
        pos += 4 + int.from_bytes(head[1:4], "big")
        if last:
            break
    assert 6 in kinds                          # FLAC PICTURE
    assert 4 in kinds                          # VORBIS_COMMENT


def test_untagged_flac_returns_empty(tmp_path):
    assert read_flac_tags(_flac_bytes(tmp_path)) == []


def test_write_tags_on_non_flac_is_reported(tmp_path):
    path = tmp_path / "not.flac"
    path.write_bytes(b"nope" * 10)
    assert write_tags(str(path), "flac", TagInfo(title="x")) == "not-flac"


def test_mp3_id3_written(tmp_path):
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 100
    path = tmp_path / "a.mp3"
    path.write_bytes(frame * 3)
    assert write_tags(str(path), "mp3", TagInfo(title="标题", artists=["艺术家"])) == "tagged"
    blob = path.read_bytes()
    assert blob[:3] == b"ID3"
    size = 0
    for b in blob[6:10]:
        size = (size << 7) | (b & 0x7F)
    assert blob[10 + size:] == frame * 3       # audio preserved verbatim
    for frame_id in (b"TIT2", b"TPE1"):
        assert frame_id in blob[10:10 + size]


def test_mp3_retag_does_not_stack(tmp_path):
    path = tmp_path / "b.mp3"
    path.write_bytes(b"\xff\xfb\x90\x00" * 40)
    tag = TagInfo(title="x")
    write_tags(str(path), "mp3", tag)
    first = path.stat().st_size
    write_tags(str(path), "mp3", tag)
    assert path.stat().st_size == first


def test_empty_tag_is_a_noop(tmp_path):
    path = _flac_bytes(tmp_path)
    before = open(path, "rb").read()
    assert write_tags(path, "flac", TagInfo()) == "no-tags"
    assert open(path, "rb").read() == before


def test_unsupported_format_is_skipped(tmp_path):
    path = tmp_path / "x.ogg"
    path.write_bytes(b"OggS" + b"\x00" * 50)
    assert write_tags(str(path), "ogg", TagInfo(title="t")) == "unsupported-format"


# --- language detection -------------------------------------------------------

@pytest.mark.parametrize("title,artists,album,lyrics,expected", [
    ("万華鏡", ["HACHI"], "", "", "zh"),                      # kanji-only is ambiguous
    ("万華鏡", ["HACHI"], "", "雪は解けて", "ja"),             # ...until the lyrics say so
    ("Aster", ["Cereus"], "", "雪は解けて消えるのに", "ja"),
    ("Aster", ["Cereus"], "", "", "other"),
    ("いつか", ["A"], "", "", "ja"),
    ("晴天", ["周杰伦"], "", "", "zh"),
    ("Heat Waves", ["Glass Animals"], "", "", "other"),
    ("", ["단비"], "", "", "other"),
    ("曲名", [], "アルバム名", "", "ja"),                     # album can carry the kana
])
def test_guess_language(title, artists, album, lyrics, expected):
    assert guess_language(title, artists, album, lyrics) == expected


def test_guess_language_lyrics_without_kana_stays_non_japanese():
    assert guess_language("Aster", ["Cereus"], "", "english lyrics only") == "other"


# --- mime sniffing ------------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    (b"\xff\xd8\xff\xe0", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
    (b"GIF89a", "image/gif"),
    (b"", "application/octet-stream"),
    (b"????", "application/octet-stream"),
])
def test_sniff_mime(data, expected):
    assert _sniff_mime(data) == expected


def test_no_personal_paths_in_module_source():
    """Guard against a machine-specific default sneaking back into the GUI."""
    import ncmdump.tags as tags

    source = open(tags.__file__, encoding="utf-8").read()
    assert not re.search(r"[A-Z]:\\\\Users|W:\\\\", source)
