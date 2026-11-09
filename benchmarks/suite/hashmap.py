"""Hash map workload on dict, the CPython reference for hashmap.vx."""

import sys

MODULUS = 2147483648


def main():
    n = int(sys.argv[1])
    space = n // 2 + 1
    m = {}
    x = 42
    for _ in range(n):
        x = (x * 1103515245 + 12345) % MODULUS
        k = x % space
        v = m.get(k)
        if v is not None:
            m[k] = v + 1
        else:
            m[k] = 1
    total = 0
    missing = 0
    for k in range(n):
        v = m.get(k)
        if v is not None:
            total = (total + v * k) % MODULUS
        else:
            missing += 1
    for k in range(n // 4):
        m.pop(k, None)
    print(total)
    print(missing)
    print(len(m))


main()
