"""Recursive Fibonacci, the CPython reference for fib.vx."""

import sys


def fib(n: int) -> int:
    return n if n < 2 else fib(n - 1) + fib(n - 2)


print(fib(int(sys.argv[1])))
