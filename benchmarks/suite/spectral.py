"""Spectral norm, the CPython reference for spectral.vx."""

import math
import sys


def a(i, j):
    return 1.0 / float((i + j) * (i + j + 1) // 2 + i + 1)


def times(v, n):
    out = []
    for i in range(n):
        total = 0.0
        for j in range(n):
            total += a(i, j) * v[j]
        out.append(total)
    return out


def times_transposed(v, n):
    out = []
    for i in range(n):
        total = 0.0
        for j in range(n):
            total += a(j, i) * v[j]
        out.append(total)
    return out


def main():
    n = int(sys.argv[1])
    u = [1.0] * n
    v = u
    for _ in range(10):
        v = times_transposed(times(u, n), n)
        u = times_transposed(times(v, n), n)
    vbv = 0.0
    vv = 0.0
    for i in range(n):
        vbv += u[i] * v[i]
        vv += v[i] * v[i]
    print(int(math.sqrt(vbv / vv) * 1000000000.0))


main()
