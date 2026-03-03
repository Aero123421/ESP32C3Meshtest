from __future__ import annotations

from dataclasses import dataclass
from math import gcd
from typing import Iterable


GF_PRIMITIVE = 0x11D
GF_SIZE = 256


def _build_gf_tables() -> tuple[list[int], list[int]]:
    exp = [0] * (GF_SIZE * 2)
    log = [0] * GF_SIZE
    value = 1
    for i in range(GF_SIZE - 1):
        exp[i] = value
        log[value] = i
        value <<= 1
        if value & GF_SIZE:
            value ^= GF_PRIMITIVE
    for i in range(GF_SIZE - 1, GF_SIZE * 2):
        exp[i] = exp[i - (GF_SIZE - 1)]
    return exp, log


GF_EXP, GF_LOG = _build_gf_tables()


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def gf_inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("gf_inv(0)")
    return GF_EXP[(GF_SIZE - 1) - GF_LOG[a]]


@dataclass(frozen=True)
class ReliableProfile:
    profile_id: int
    name: str
    data_shards: int
    parity_shards: int
    shard_size: int

    @property
    def total_shards(self) -> int:
        return self.data_shards + self.parity_shards

    @property
    def max_payload_bytes(self) -> int:
        return self.data_shards * self.shard_size


RELIABLE_PROFILES: dict[int, ReliableProfile] = {
    0: ReliableProfile(profile_id=0, name="25+8", data_shards=25, parity_shards=8, shard_size=40),
    1: ReliableProfile(profile_id=1, name="25+10", data_shards=25, parity_shards=10, shard_size=40),
}


def get_profile(profile_id: int) -> ReliableProfile:
    profile = RELIABLE_PROFILES.get(int(profile_id))
    if profile is None:
        raise ValueError(f"unsupported reliable profile id: {profile_id}")
    return profile


def _row_for_index(index: int, profile: ReliableProfile) -> list[int]:
    k = profile.data_shards
    if index < 0 or index >= profile.total_shards:
        raise ValueError(f"shard index out of range: {index}")
    if index < k:
        row = [0] * k
        row[index] = 1
        return row
    base = (index - k) + 1
    row = [1] * k
    for col in range(1, k):
        row[col] = gf_mul(row[col - 1], base)
    return row


def _invert_matrix(matrix: list[list[int]]) -> list[list[int]] | None:
    n = len(matrix)
    a = [row[:] for row in matrix]
    inv = [[0] * n for _ in range(n)]
    for i in range(n):
        inv[i][i] = 1

    for col in range(n):
        pivot = col
        while pivot < n and a[pivot][col] == 0:
            pivot += 1
        if pivot >= n:
            return None
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
            inv[col], inv[pivot] = inv[pivot], inv[col]

        pivot_val = a[col][col]
        inv_pivot = gf_inv(pivot_val)
        for j in range(n):
            a[col][j] = gf_mul(a[col][j], inv_pivot)
            inv[col][j] = gf_mul(inv[col][j], inv_pivot)

        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            if factor == 0:
                continue
            for j in range(n):
                a[row][j] ^= gf_mul(factor, a[col][j])
                inv[row][j] ^= gf_mul(factor, inv[col][j])
    return inv


def encode_shards(payload: bytes, profile: ReliableProfile) -> list[bytes]:
    if len(payload) > profile.max_payload_bytes:
        raise ValueError(f"payload too large: {len(payload)} > {profile.max_payload_bytes}")
    shard_size = profile.shard_size
    k = profile.data_shards
    m = profile.parity_shards

    padded = payload + (b"\x00" * (profile.max_payload_bytes - len(payload)))
    data_shards: list[bytearray] = []
    for idx in range(k):
        start = idx * shard_size
        data_shards.append(bytearray(padded[start : start + shard_size]))

    parity_shards: list[bytearray] = [bytearray(shard_size) for _ in range(m)]
    for p in range(m):
        row = _row_for_index(k + p, profile)
        out = parity_shards[p]
        for col in range(k):
            coeff = row[col]
            if coeff == 0:
                continue
            source = data_shards[col]
            if coeff == 1:
                for b in range(shard_size):
                    out[b] ^= source[b]
            else:
                for b in range(shard_size):
                    out[b] ^= gf_mul(coeff, source[b])

    out_shards = [bytes(shard) for shard in data_shards]
    out_shards.extend(bytes(shard) for shard in parity_shards)
    return out_shards


def decode_shards(
    shard_map: dict[int, bytes],
    profile: ReliableProfile,
    *,
    original_size: int,
) -> bytes | None:
    if original_size < 0 or original_size > profile.max_payload_bytes:
        raise ValueError(f"invalid original_size: {original_size}")
    if len(shard_map) < profile.data_shards:
        return None

    shard_size = profile.shard_size
    available = sorted(idx for idx in shard_map.keys() if 0 <= idx < profile.total_shards)
    if len(available) < profile.data_shards:
        return None
    selected = available[: profile.data_shards]
    selected_rows = [_row_for_index(idx, profile) for idx in selected]
    inv = _invert_matrix(selected_rows)
    if inv is None:
        return None

    selected_shards: list[bytes] = []
    for idx in selected:
        shard = shard_map.get(idx)
        if not isinstance(shard, (bytes, bytearray)) or len(shard) != shard_size:
            return None
        selected_shards.append(bytes(shard))

    data_out: list[bytearray] = [bytearray(shard_size) for _ in range(profile.data_shards)]
    for out_row in range(profile.data_shards):
        coeffs = inv[out_row]
        out = data_out[out_row]
        for src_row, coeff in enumerate(coeffs):
            if coeff == 0:
                continue
            source = selected_shards[src_row]
            if coeff == 1:
                for b in range(shard_size):
                    out[b] ^= source[b]
            else:
                for b in range(shard_size):
                    out[b] ^= gf_mul(coeff, source[b])

    raw = b"".join(bytes(shard) for shard in data_out)
    return raw[:original_size]


def missing_shard_indexes(total_shards: int, present: Iterable[int]) -> list[int]:
    present_set = {int(v) for v in present}
    return [idx for idx in range(max(0, int(total_shards))) if idx not in present_set]


def interleaved_indexes(total_shards: int, *, stride: int = 7) -> list[int]:
    total = max(0, int(total_shards))
    if total <= 1:
        return [0] if total == 1 else []
    step = max(1, int(stride))
    while gcd(step, total) != 1:
        step += 1
    order: list[int] = []
    seen: set[int] = set()
    idx = 0
    for _ in range(total):
        while idx in seen:
            idx = (idx + 1) % total
        order.append(idx)
        seen.add(idx)
        idx = (idx + step) % total
    return order
