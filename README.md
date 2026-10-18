# Agent-Ready Observability: Logging for Machines, Not Humans

A demo of an AI agent that investigates production incidents: it reads logs and source code, follows a
`trace_id` through interleaved logs to the line of code that failed, and writes an incident report.
Everything runs locally: the agent uses a small open model via [Ollama](https://ollama.com), so no logs or
code leave the machine.

The point of the demo: the agent is only as good as the telemetry. The same code runs with two logging setups,
plain text and structured, and the agent investigates both logs.

## The program

The Python code lives in [`demo/`](demo). The logic is in one file,
[`demo/chain_divide/chain_divide.py`](demo/chain_divide/chain_divide.py): it reads number sets from
[`config/chain-divide.toml`](config/chain-divide.toml), divides the numbers of each set one by
another (integer division) and uses the result as the key of a label from the same config. Several runs execute
in parallel threads, each with its own set, so log lines of different runs are interleaved; like OpenTelemetry
instrumentation, every run gets its own trace context (`Trace` in a `contextvars.ContextVar`).

Two things can go wrong, and both depend only on the data in the config:

- a set contains 0 as a divisor: `ZeroDivisionError: division by zero`;
- the result is not a key in the labels: `labels.get(key)` returns `None` and `.upper()` throws
  `AttributeError: 'NoneType' object has no attribute 'upper'`.

**The config is hidden from the agent**, like a production config. From the code it can see where things *could*
fail; which data broke which run, it can learn only from the logs.

The logic logs only through the standard `logging` module: a human-readable message, and in `extra` an event
name and the data as key-value pairs.

```python
log.info("Loaded number #%d: %d", position, number,
         extra={"event": "number.loaded", "data": {"position": position, "value": number}})

log.exception("Chain divide failed: %s", error,
              extra={"event": "chain_divide.failed", "data": {"config.set": name}})
```

What ends up in the log is decided by the logging setup, one per entry point:

| Entry point | Log | What it writes |
|---|---|---|
| [`demo/simple_logging.py`](demo/simple_logging.py) | `logs/chain-divide-simple.log` | plain text: timestamp, level, message; drops event names, data, the trace context and stack traces |
| [`demo/improved_logging.py`](demo/improved_logging.py) | `logs/chain-divide.log` | one JSON object per line with everything |

Each entry point also runs on its own from the repository root: `python demo/improved_logging.py [runs]`.

## Two kinds of logs

`logs/chain-divide-simple.log`:

```
2026-10-22 09:43:36 INFO Loaded number #1: 12
2026-10-22 09:43:36 INFO Loaded number #1: 100
2026-10-22 09:43:36 INFO Loaded number #1: 64
2026-10-22 09:43:36 INFO Loaded number #1: 81
2026-10-22 09:43:36 ERROR Chain divide failed: division by zero
```

The data is there, but with parallel runs nothing tells which numbers belong to which error, and there is no
stack trace, so not even the failing line is known.

`logs/chain-divide.log`:

```json
{"timestamp":"2026-10-22T08:43:36.832720Z","level":"INFO","service.name":"chain-divide","service.version":"1.4.2",
 "trace_id":"c9b6bfa834edf11a98e0e6054396d1b6","span_id":"ec6cff067bbd490c","event":"number.loaded",
 "message":"Loaded number #1: 12","position":1,"value":12,
 "code.file.path":"demo/chain_divide/chain_divide.py","code.line.number":90,"code.function.name":"chain_divide.chain_divide"}
```

A handful of fields turns an investigation into a series of joins:

| Field | Lets the agent... |
|---|---|
| `event` (`number.loaded`, `label.lookup`, `chain_divide.failed`) | count and group without parsing free text |
| `trace_id`, `span_id` | join the log lines of one run |
| `service.name`, `service.version` | tell which build produced the line |
| `code.file.path`, `code.line.number`, `code.function.name` | go from a log line to the code |
| `exception.type`, `exception.message`, `exception.cause_chain`, `exception.stacktrace` | find the failing line and the root cause |
| key-value pairs (`position`, `value`, `key`, `config.set` on errors) | see the data the code processed |

Field names follow the [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
where one exists. There are no dependencies: the entry points plug a `logging.Formatter` into the standard
`logging` module, and the JSON setup also adds a `logging.Filter` that copies the trace context of the current run
into every record. In a real service a JSON logging library and the OpenTelemetry instrumentation do this. File
paths in `code.file.path` and in the traceback are relative to the repository root, so the agent can open them
directly.

## The agent

The agent (Python, [LangChain](https://python.langchain.com) + Ollama) runs a four-step pipeline for every
log file in `logs/`:

| Step | What happens | LLM? |
|---|---|---|
| **Detect** | Parse the log (JSON or plain text), find errors, extract the exception and our own stack frames, group repeated errors into one incident | no, plain code |
| **Route** | Classify the incident (category, severity); resolve the owning team from a CODEOWNERS-like map | partly |
| **Investigate** | A tool-using agent reads the code and pulls the logs of affected runs by `trace_id` | yes |
| **Report** | Write a Markdown report: facts (log file, counts, timestamps, owner) come from code, hypothesis and fix from the LLM | yes |

### Agent tools

| Tool | Purpose |
|---|---|
| `get_trace(trace_id)` | All log lines of one run, in order; for errors, the exception and our own stack frames |
| `search_logs(pattern)` | Regex search over the log being investigated |
| `read_file(path, start, end)` | Read source code with line numbers |
| `search_code(pattern)` | Regex search over the source code |
| `list_files()` | List project source files |

The file tools only see `demo/chain_divide/`, the code under investigation (`SOURCE_DIRS` in `sre_agent/utils.py`),
so neither the entry points with their logging setups, nor the config, nor the logs can be read as files. The logs are
reachable only through `get_trace` and `search_logs`, and only the log being investigated, so the agent cannot
borrow data from the other log.

For an error in `chain-divide.log`, `get_trace` shows the whole run:

```
19: 2026-10-22T08:43:36.834003Z INFO Loaded number #1: 90
38: 2026-10-22T08:43:36.850856Z INFO Loaded number #2: 3
51: 2026-10-22T08:43:36.858053Z INFO Loaded number #3: 5
61: 2026-10-22T08:43:36.862738Z INFO Looking up label for 6
62: 2026-10-22T08:43:36.863017Z ERROR Chain divide failed: 'NoneType' object has no attribute 'upper'
Traceback (most recent call last):
  File "demo/chain_divide/chain_divide.py", line 69, in run
    self.label(name)
  File "demo/chain_divide/chain_divide.py", line 83, in label
    label = name.upper()
AttributeError: 'NoneType' object has no attribute 'upper'
```

If the agent finishes without calling `get_trace` while the incident has trace ids, it is asked to look at the
logs of the affected runs and answer again. If it hits the step limit, it summarizes what it has found so far
instead of returning nothing.

### What to expect

On `chain-divide.log` the agent reports two incidents, each with the failing line and the data behind it: which
sets contain a 0 (`demo/chain_divide/chain_divide.py:96`), and which division results have no label (`:83`).

On `chain-divide-simple.log` both failures merge into one incident with an unknown location and the `@on-call`
owner. The agent can find the failing line by reading the code, but it cannot tell which data caused which
error. It often misses one of the two failures, or invents a cause, and it may spend its step limit looking
for trace ids that are not there.

In both logs the model sometimes tries `read_file("config/chain-divide.toml")` to look for the bad data. The tool
refuses: the config is outside `SOURCE_DIRS`.

## Setup

Requirements: Python 3.11+ (tested with 3.14) and [Ollama](https://ollama.com). The demo program uses only the
standard library; the requirements are for the agent.

```bash
ollama pull qwen3.5:4b

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running the demo

One command from the repository root drops `logs/` and `reports/`, writes both logs and runs the agent:

```bash
make
```

The steps also run on their own: `make clean`, `make logs`, `make agent`. Variables:

| Variable | Default | Meaning |
|---|---|---|
| `RUNS` | `20` | Parallel runs per log |
| `PYTHON` | `.venv/bin/python` | Python with the agent's requirements |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_MODEL` | `qwen3.5:4b` | Any Ollama model with tool calling |
| `AGENT_THINK` | off | Set to `1` to enable the model's thinking mode (slower) |

```bash
AGENT_MODEL=qwen3.5:35b .venv/bin/python -m sre_agent.agent
```

`qwen3.5:4b` is fast but sometimes gets details wrong, e.g. it mistypes a trace id or sets `confidence: high`
without evidence. `qwen3.5:35b` (a Mixture-of-Experts model, ~3B active parameters, 24 GB download) is noticeably
more accurate and still reasonably fast on a machine with enough memory.

## Project layout

```
Makefile                make: clean -> logs -> agent
demo/
  chain_divide/
    chain_divide.py     The logic and the Trace context
  simple_logging.py     Entry point: plain text logs -> logs/chain-divide-simple.log
  improved_logging.py   Entry point: JSON logs -> logs/chain-divide.log
config/
  chain-divide.toml     Number sets and labels; hidden from the agent
sre_agent/
  agent.py              Detect -> Route -> Investigate -> Report pipeline
  utils.py              Log parsing and agent tools
  models.py             Data classes and structured-output schemas
  report.md.j2          Jinja2 template of the incident report
logs/                   Logs the agent analyzes (created by the entry points)
reports/                Generated incident reports, one directory per log
```
