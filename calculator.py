#!/usr/bin/env python3
"""Simple command-line calculator."""

import argparse
import operator

OPERATIONS = {
    "add": operator.add,
    "sub": operator.sub,
    "mul": operator.mul,
    "div": operator.truediv,
}


def calculate(operation: str, left: float, right: float) -> float:
    if operation == "div" and right == 0:
        raise ValueError("cannot divide by zero")
    return OPERATIONS[operation](left, right)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple calculator")
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("left", type=float)
    parser.add_argument("right", type=float)
    args = parser.parse_args()
    print(calculate(args.operation, args.left, args.right))


if __name__ == "__main__":
    main()
