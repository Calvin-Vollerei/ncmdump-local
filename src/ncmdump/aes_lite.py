# -*- coding: utf-8 -*-
"""Minimal AES-128/192/256 ECB decryptor (pure Python, no third-party modules).

Only what the NCM format needs: AES-ECB decryption + PKCS#7 unpadding.
The S-box / key schedule follow FIPS-197.
"""
from __future__ import annotations

# --- FIPS-197 tables (generated once at import; a few ms) -------------------

def _gen_tables():
    sbox = [0] * 256
    inv_sbox = [0] * 256
    p = q = 1
    while True:
        # multiply p by 3 in GF(2^8)
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if (p & 0x80) else 0)
        # divide q by 3 (multiply by 0xf6)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox


SBOX, INV_SBOX = _gen_tables()
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a


def _mul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


class AES:
    """AES block cipher, decryption direction only."""

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key must be 16/24/32 bytes, got %d" % len(key))
        self.nk = len(key) // 4
        self.nr = self.nk + 6
        self._round_keys = self._expand(key)

    def _expand(self, key: bytes) -> list:
        nk, nr = self.nk, self.nr
        w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        for i in range(nk, 4 * (nr + 1)):
            temp = list(w[i - 1])
            if i % nk == 0:
                temp = temp[1:] + temp[:1]
                temp = [SBOX[b] for b in temp]
                temp[0] ^= RCON[i // nk - 1]
            elif nk > 6 and i % nk == 4:
                temp = [SBOX[b] for b in temp]
            w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
        # group into round keys
        rks = []
        for r in range(nr + 1):
            rk = []
            for c in range(4):
                rk.extend(w[r * 4 + c])
            rks.append(rk)
        return rks

    def _decrypt_block(self, block: bytes) -> bytes:
        s = list(block)
        rk = self._round_keys
        # AddRoundKey (last round key)
        for i in range(16):
            s[i] ^= rk[self.nr][i]
        for rnd in range(self.nr - 1, 0, -1):
            # InvShiftRows
            s = [
                s[0], s[13], s[10], s[7],
                s[4], s[1], s[14], s[11],
                s[8], s[5], s[2], s[15],
                s[12], s[9], s[6], s[3],
            ]
            # InvSubBytes
            s = [INV_SBOX[b] for b in s]
            # AddRoundKey
            key = rk[rnd]
            for i in range(16):
                s[i] ^= key[i]
            # InvMixColumns
            ns = [0] * 16
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                ns[4 * c + 0] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
                ns[4 * c + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
                ns[4 * c + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
                ns[4 * c + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)
            s = ns
        # final round
        s = [
            s[0], s[13], s[10], s[7],
            s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15],
            s[12], s[9], s[6], s[3],
        ]
        s = [INV_SBOX[b] for b in s]
        key = rk[0]
        for i in range(16):
            s[i] ^= key[i]
        return bytes(s)


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """Decrypt an ECB stream. Trailing partial block is dropped (as crypto-js does)."""
    aes = AES(key)
    out = bytearray()
    n = len(data) - (len(data) % 16)
    for i in range(0, n, 16):
        out += aes._decrypt_block(data[i:i + 16])
    return bytes(out)


def pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding; returns data unchanged if padding looks invalid."""
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


# --- self-test --------------------------------------------------------------

def _selftest():
    """FIPS-197 appendix C vectors (encrypt via inverse check is not possible here,
    so we verify against known decrypt vectors)."""
    vectors = [
        # key, ciphertext, plaintext
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
    for k, c, p in vectors:
        got = aes_ecb_decrypt(bytes.fromhex(k), bytes.fromhex(c)).hex()
        assert got == p, "AES self-test failed: %s != %s" % (got, p)
    return True


if __name__ == "__main__":
    _selftest()
    print("AES-128/192/256 ECB decrypt self-test: OK")
