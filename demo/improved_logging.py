"""
Runs chain_divide with agent-ready logging: one JSON object per line with a stable event name,
trace_id/span_id, the service version, file:line of the log call, the data as key-value pairs
and the exception with its cause chain and stack trace.

Run from the repository root: python demo/improved_logging.py [runs]   (no dependencies)
"""
import json
import logging
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

from chain_divide.chain_divide import ChainDivide, TRACE

SERVICE = "chain-divide"
VERSION = "1.4.2"
LOG_FILE = Path("logs", "chain-divide.log")


class TraceFilter(logging.Filter):
    """Puts the trace context of the current run into the record, like OpenTelemetry's logging instrumentation."""

    def filter(self, record: logging.LogRecord) -> bool:
        if trace := TRACE.get(None):
            record.trace_id, record.span_id = trace.trace_id, trace.span_id
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Field names follow the OpenTelemetry semantic conventions where one exists."""

    def format(self, record: logging.LogRecord) -> str:
        # Fixed width, UTC: log lines sort as plain strings
        line = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "service.name": SERVICE,
            "service.version": VERSION,
        }
        if hasattr(record, "trace_id"):
            line["trace_id"], line["span_id"] = record.trace_id, record.span_id
        line["event"] = getattr(record, "event", None)
        line["message"] = record.getMessage()
        line.update(getattr(record, "data", {}))
        # file:line of the log call, relative to the repository root
        line["code.file.path"] = os.path.relpath(record.pathname)
        line["code.line.number"] = record.lineno
        line["code.function.name"] = f"{record.module}.{record.funcName}"
        if record.exc_info:
            error = record.exc_info[1]
            line["exception.type"] = type(error).__qualname__
            line["exception.message"] = str(error)
            line["exception.cause_chain"] = cause_chain(error)
            line["exception.stacktrace"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str, separators=(",", ":"))

    def formatException(self, ei) -> str:
        # file paths relative to the repository root, as in code.file.path
        return super().formatException(ei).replace(f'File "{os.getcwd()}{os.sep}', 'File "')


def cause_chain(error: BaseException) -> list[str]:
    """"type: message" of the exception and each of its causes, outermost first, root cause last."""
    chain = []
    while error:
        chain.append(f"{type(error).__qualname__}: {error}")
        error = error.__cause__ or error.__context__
    return chain


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    LOG_FILE.parent.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, mode="w")
    handler.addFilter(TraceFilter())
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    failed = ChainDivide().run_in_parallel(runs)
    print(f"{runs} runs, {failed} failed -> {LOG_FILE}")


if __name__ == "__main__":
    main()
