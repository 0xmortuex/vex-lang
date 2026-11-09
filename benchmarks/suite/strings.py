"""String workload, the CPython reference for strings.vx."""

import sys

MODULUS = 2147483647


def main():
    n = int(sys.argv[1])
    h = 7
    hits = 0
    total = 0
    for i in range(n):
        s = f"item-{i}-{i * 7}"
        parts = s.split("-")
        if "99" in s:
            hits += 1
        u = s.upper()
        total += len(u) + len(parts[1])
        for c in list(u):
            h = (h * 31 + ord(c)) % MODULUS
    print(h)
    print(hits)
    print(total)


main()
