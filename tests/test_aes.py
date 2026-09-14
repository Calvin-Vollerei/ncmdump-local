# -*- coding: utf-8 -*-
"""AES self-tests: FIPS-197 vectors plus a random round-trip against the package's own
container builder (which encrypts using the same S-box)."""

import os

import pytest

from ncmdump.aes_lite import AES, aes_ecb_decrypt, pkcs7_unpad

# FIPS-197 appendix C, decryption direction
FIPS197 = [
    ("000102030405060708090a0b0c0d0e0f",
     "69c4e0d86a7b0430d8cdb78070b4c55a",
     "00112233445566778899aabbccddeeff"),
    ("000102030405060708090a0b0c0d0e0f1011121314151617",
     "dda97ca4864cdfe06eaf70a0ec0d7191",
     "00112233445566778899aabbccddeeff"),
    ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
     "8ea2b7ca516745bfeafc49904b496089",
     "00112233445566778899aabbccddeeff"),
]


@pytest.mark.parametrize("key,ct,pt", FIPS197)
def test_fips197_decrypt(key, ct, pt):
    assert aes_ecb_decrypt(bytes.fromhex(key), bytes.fromhex(ct)).hex() == pt


def test_selftest_hook_runs():
    from ncmdump import aes_lite

    assert aes_lite._selftest() is True


def test_builder_encryptor_matches_openssl():
    """The fixture builder's encryptor must be real AES, not an approximation.

    Vector produced with:
        openssl enc -aes-128-ecb -K 000102030405060708090a0b0c0d0e0f -nopad ...
    (the builder emits a second block because of PKCS#7 padding, so only block 1 is
    compared here; the padding itself is covered by the round-trip test below).
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    import ncm_builder

    key = bytes(range(16))
    plain = b"0123456789abcdef"
    expected_first_block = "281567ab2f4cf0d73d3198225b8b8393"
    assert ncm_builder._aes_ecb_encrypt(key, plain).hex().startswith(expected_first_block)


def test_random_roundtrip_against_builder():
    """Encrypt with the test builder, decrypt with the package: must be the identity.

    Note ``aes_ecb_decrypt`` only decrypts — stripping PKCS#7 is ``pkcs7_unpad``'s job,
    which is why both are applied here.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    import ncm_builder

    key = os.urandom(16)
    for size in (16, 32, 64, 100):
        plain = os.urandom(size)
        cipher = ncm_builder._aes_ecb_encrypt(key, plain)
        assert pkcs7_unpad(aes_ecb_decrypt(key, cipher)) == plain


def test_builder_encryptor_rejects_long_keys():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    import ncm_builder

    with pytest.raises(ValueError):
        ncm_builder._aes_ecb_encrypt(bytes(24), b"x")


def test_invalid_key_length():
    with pytest.raises(ValueError):
        AES(b"short")


def test_partial_trailing_block_is_dropped():
    key = bytes(range(16))
    # 20 bytes in: only the first full block is processed, as crypto-js does
    out = aes_ecb_decrypt(key, bytes(20))
    assert len(out) == 16


@pytest.mark.parametrize("data,expected", [
    (b"\x01", b""),                       # single pad byte
    (b"abc\x01", b"abc"),
    (b"abc" + b"\x03" * 3, b"abc"),
    (b"", b""),                           # empty stays empty
    (b"abc", b"abc"),                     # no valid padding -> unchanged
    (b"abc\x00", b"abc\x00"),             # padding byte 0 is invalid
    (b"abc\x11", b"abc\x11"),             # 17 is out of range
])
def test_pkcs7_unpad(data, expected):
    assert pkcs7_unpad(data) == expected


def test_pkcs7_unpad_full_block():
    block = b"0123456789abcdef"
    assert pkcs7_unpad(block + b"\x10" * 16) == block
