import json
import re
from pathlib import Path

from langchain_core.tools import tool

from sre_agent.models import LogRecord

ROOT = Path(__file__).parent.parent.resolve()
LOG_DIR = ROOT / "logs"
REPORTS_DIR = ROOT / "reports"
# The only directory the file tools (read_file, list_files, search_code) can see: the code under investigation.
# The entry points in demo/ (logging setups) are not here, nor are the logs: those are reachable only through
# get_trace / search_logs, and only the current one.
SOURCE_DIRS = [ROOT / "demo" / "chain_divide"]

# "2026-10-17 16:45:25 INFO Generated number: 3" (plain text logs, no trace id)
PLAIN_RECORD = re.compile(r"^(\d{4}-\d{2}-\d{2} [\d:.,]+) (\w+) (.*)")
# '  File "demo/chain_divide/chain_divide.py", line 96, in chain_divide' -> file, line, function
FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)', re.MULTILINE)


log_file = LOG_DIR / "chain-divide.log"  # the log the agent tools read; the pipeline switches it per file


def use_log(path: Path):
    global log_file
    log_file = path


def parse_log(path: Path | None = None) -> list[LogRecord]:
    """
    Parse a log file into records. JSON lines: the stack trace of an error is appended to its text.
    Plain text lines (`timestamp LEVEL message`): no trace id; other lines are glued to the previous record.
    """
    path = path or log_file
    records: list[LogRecord] = []
    if not path.exists():
        return records
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith("{"):
            data = json.loads(line)
            text = data["message"] + ("\n" + data["exception.stacktrace"].rstrip() if "exception.stacktrace" in data else "")
            records.append(LogRecord(lineno, data["timestamp"], data["level"], data["service.name"],
                                     data.get("trace_id", "-"), text))
        elif match := PLAIN_RECORD.match(line):
            ts, level, text = match.groups()
            records.append(LogRecord(lineno, ts, level, path.stem, "-", text))
        elif records:  # continuation of a multi-line record
            records[-1].text += "\n" + line
    return records


def project_frames(text: str) -> list[tuple[str, int, str]]:
    """Extract (file, line, func) stack frames that point to our own code, skipping libraries, outermost first."""
    frames = []
    for file, line, func in FRAME.findall(text):
        path = (ROOT / file).resolve()
        if any(path.is_relative_to(source_dir) for source_dir in SOURCE_DIRS):
            frames.append((str(path.relative_to(ROOT)), int(line), func))
    return frames  # a Python traceback lists the outermost frame first


def _safe_path(path: str) -> Path:
    """Resolve a project path; refuse anything outside the source directories."""
    resolved = (ROOT / path).resolve()
    if not any(resolved.is_relative_to(source_dir) for source_dir in SOURCE_DIRS):
        raise ValueError(f"access denied: {path}")
    return resolved


@tool
def list_files() -> str:
    """List source files of the project."""
    files = [
        str(file_path.relative_to(ROOT)) for source_dir in SOURCE_DIRS for file_path in source_dir.rglob("*")
        if file_path.is_file() and file_path.suffix in {".py", ".toml", ".txt", ".cfg", ".yaml", ".yml"}
    ]
    return "\n".join(sorted(files))


@tool
def read_file(path: str, start: int = 1, end: int = 0) -> str:
    """Read a project file with line numbers. `end=0` means up to 200 lines from `start`."""
    try:
        lines = _safe_path(path).read_text().splitlines()
    except Exception as error:
        return f"error: {error}"
    end = end or start + 199
    return "\n".join(f"{lineno:>4} | {lines[lineno - 1]}" for lineno in range(max(1, start), min(end, len(lines)) + 1))


@tool
def search_code(pattern: str) -> str:
    """Regex search over project source files. Returns `file:line: text` matches."""
    out = []
    for file in list_files.invoke({}).splitlines():
        for lineno, line in enumerate((ROOT / file).read_text().splitlines(), 1):
            if re.search(pattern, line):
                out.append(f"{file}:{lineno}: {line.strip()}")
    return "\n".join(out[:30]) or "no matches"


@tool
def get_trace(trace_id: str) -> str:
    """
    Get ALL log lines of one run by its trace_id, in order. Logs of concurrent runs
    are interleaved in the log, so this is the only reliable way to see what one run did.
    """
    trace_id = trace_id.removeprefix("trace=")
    if trace_id in ("", "-"):
        return "no trace id given: these log lines carry no trace id"
    lines = []
    for record in parse_log():
        if record.trace_id == trace_id:
            text = record.text.splitlines()
            if len(text) > 1:  # traceback: keep our own frames with their source line, and the exception line
                text = [text[0], text[1],
                        *(line for i, line in enumerate(text[2:-1], 2) if project_frames(line) or project_frames(text[i - 1])),
                        text[-1]]
            lines.append(f"{record.lineno}: {record.ts} {record.level} {text[0]}")
            lines += text[1:]
    return "\n".join(lines) or f"no log lines for trace {trace_id}"


@tool
def search_logs(pattern: str) -> str:
    """
    Regex search over the log. Returns total match count and up to 20 last matching lines.
    Every line contains trace=<id>; pass it to get_trace to see the whole run.
    """
    lines = [f"{record.lineno}: {record.ts} {record.level} trace={record.trace_id} {record.text.splitlines()[0]}"
             for record in parse_log()]
    hits = [line for line in lines if re.search(pattern, line)]
    return f"{len(hits)} matches\n" + "\n".join(hits[-20:])


def step(name: str, text: str = ""):
    """Print a colored pipeline step header."""
    print(f"\n\033[1;36m━━ {name} ━━\033[0m {text}")
