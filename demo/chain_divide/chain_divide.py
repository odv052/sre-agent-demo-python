"""
Reads number sets from config/chain-divide.toml, divides the numbers of each set one by another
and uses the result as the key of a label from the same config.
"""
import contextvars
import logging
import random
import secrets
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path("config", "chain-divide.toml")

# What the logic needs from logging: a message, an event name and the data as key-value pairs (`extra`).
# How (and whether) each part ends up in the log is up to the logging setup, see simple_logging and improved_logging.
log = logging.getLogger("chain_divide")


@dataclass(frozen=True)
class Trace:
    """
    trace_id / span_id of the current run (W3C Trace Context sizes). In a real service OpenTelemetry creates them,
    propagates them to other services and puts them into the logging context.
    """
    trace_id: str
    span_id: str

    @staticmethod
    def random() -> "Trace":
        return Trace(secrets.token_hex(16), secrets.token_hex(8))


TRACE: contextvars.ContextVar[Trace] = contextvars.ContextVar("trace")


class ChainDivide:

    def __init__(self):
        self.sets: dict[str, list[int]] = {}
        self.labels: dict[int, str] = {}

    def run_in_parallel(self, runs: int) -> int:
        """
        Several runs in parallel, each with its own number set (round-robin), so log lines of different runs get
        interleaved. Like OpenTelemetry instrumentation, every run gets its own trace context; whether it reaches
        the log is up to the logging setup. Returns the number of failed runs.
        """
        self.load_config()
        names = list(self.sets)
        with ThreadPoolExecutor(max_workers=runs) as executor:
            results = [executor.submit(contextvars.copy_context().run, self.traced_run, names[i % len(names)])
                       for i in range(runs)]
        return sum(not result.result() for result in results)

    def load_config(self):
        config = tomllib.loads(CONFIG_FILE.read_text())
        self.sets = config["sets"]
        self.labels = {int(key): name for key, name in config["labels"].items()}

    def traced_run(self, name: str) -> bool:
        TRACE.set(Trace.random())
        return self.run(name)

    def run(self, name: str) -> bool:
        try:
            self.label(name)
            return True
        except Exception as error:
            log.exception("Chain divide failed: %s", error,
                          extra={"event": "chain_divide.failed", "data": {"config.set": name}})
            return False

    def label(self, name: str) -> str:
        """Divides the numbers of the set and returns the label for the result."""
        key = self.chain_divide(self.sets[name])
        log.info("Looking up label for %s", key, extra={"event": "label.lookup", "data": {"key": key}})
        time.sleep(random.randrange(20) / 1000)

        name = self.labels.get(key)
        label = name.upper()
        log.info("Label: %s", label, extra={"event": "label.found", "data": {"key": key, "label": label}})
        return label

    def chain_divide(self, numbers: list[int]) -> int:
        """Divides the numbers one by another."""
        for position, number in enumerate(numbers, 1):
            log.info("Loaded number #%d: %d", position, number,
                     extra={"event": "number.loaded", "data": {"position": position, "value": number}})
            time.sleep(random.randrange(20) / 1000)

        result = numbers[0]
        for divisor in numbers[1:]:
            result = result // divisor
        return result
