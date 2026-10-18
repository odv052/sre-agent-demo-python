"""
Runs chain_divide with the simplest logging: plain text, timestamp, level, message.
Event names, key-value data, the trace context and stack traces are dropped. Fine for a human reading one run;
with parallel runs nothing tells which line belongs to which run.

Run from the repository root: python demo/simple_logging.py [runs]   (no dependencies)
"""
import logging
import sys
from pathlib import Path

from chain_divide.chain_divide import ChainDivide

LOG_FILE = Path("logs", "chain-divide-simple.log")


class TextFormatter(logging.Formatter):
    """`2026-10-18 17:18:54 INFO Loaded number #1: 100`"""

    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    def formatException(self, ei) -> str:
        return ""  # no stack traces


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    LOG_FILE.parent.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, mode="w")
    handler.setFormatter(TextFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    failed = ChainDivide().run_in_parallel(runs)
    print(f"{runs} runs, {failed} failed -> {LOG_FILE}")


if __name__ == "__main__":
    main()
