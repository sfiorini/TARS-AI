#!/usr/bin/env python3
"""roll_dice.py [count] [sides] — prints e.g. 'Rolled 2d6: 4, 1 (total 5)'."""
import random
import sys


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    sides = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    if not (1 <= count <= 10) or not (2 <= sides <= 100):
        print("count must be 1-10 and sides must be 2-100", file=sys.stderr)
        sys.exit(1)                      # exercises the error contract
    rolls = [random.randint(1, sides) for _ in range(count)]
    print(f"Rolled {count}d{sides}: {', '.join(map(str, rolls))} (total {sum(rolls)})")


if __name__ == "__main__":
    main()
