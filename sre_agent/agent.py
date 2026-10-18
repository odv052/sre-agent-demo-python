import os
import re
import time
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain_ollama import ChatOllama
from jinja2 import Environment, FileSystemLoader
from langgraph.errors import GraphRecursionError

from sre_agent.models import Incident, Triage, Report, LogRecord
from sre_agent.utils import parse_log, list_files, read_file, search_code, search_logs, get_trace, ROOT, \
    REPORTS_DIR, LOG_DIR, project_frames, step, use_log

OWNERS = {"demo/chain_divide/chain_divide.py": "@backend-team"}
DEFAULT_OWNER = "@on-call"

llm = ChatOllama(
    model=os.getenv("AGENT_MODEL", "qwen3.5:4b"),
    # model=os.getenv("AGENT_MODEL", "qwen3.5:35b"),
    temperature=0,
    reasoning=os.getenv("AGENT_THINK") == "1",
)

# ───────────────────────────── 1. DETECT ─────────────────────────────
# No LLM here: log parsing, grouping and deduplication are done in plain Python.
EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))(?::\s*(.*))?$")


def detect(records: list[LogRecord], after_line: int = 0) -> list[Incident]:
    incidents: dict[str, Incident] = {}
    for rec in records:
        if rec.lineno <= after_line or rec.level not in ("ERROR", "CRITICAL"):
            continue
        # Python prints the exception in the last line of the traceback
        exc_line = rec.text.splitlines()[-1].strip() if "\n" in rec.text else ""
        match = EXC_LINE.match(exc_line)
        exc_type, exc_msg = (match[1], match[2] or "") if match else ("LoggedError", rec.text.splitlines()[0])
        frames = project_frames(rec.text)
        where = f"{frames[-1][0]}:{frames[-1][2]}" if frames else rec.logger
        sig = f"{exc_type}@{where}"
        inc = incidents.setdefault(sig, Incident(sig, exc_type, exc_msg, frames))
        inc.records.append(rec)
    return list(incidents.values())


# ───────────────────────────── 2. ROUTE ──────────────────────────────
# The LLM triages (category/severity); the owner is resolved by code.


def route(inc: Incident) -> tuple[Triage, str]:
    prompt = (
        "You triage backend production errors. Classify this incident.\n"
        "- code_bug: a defect in our code\n"
        "- bad_input: client sent invalid data and our code did not handle it\n"
        "- infrastructure: DB/network/disk/external service failure\n\n"
        f"Exception: {inc.exc_type}: {inc.exc_message}\n"
        f"Location: {inc.location}\n"
        f"Occurrences: {len(inc.records)}\n\n"
        f"Log excerpt:\n{inc.records[0].text[-1500:]}"
    )
    triage = llm.with_structured_output(Triage, method="json_schema").invoke(prompt)
    file = inc.frames[-1][0] if inc.frames else ""
    return triage, OWNERS.get(file, DEFAULT_OWNER)


# ─────────────────────────── 3. INVESTIGATE ──────────────────────────
# Tool-using agent: reads code and logs, builds a hypothesis.


INVESTIGATOR_PROMPT = """You are a senior backend engineer investigating a production incident.
You get the exception, the stack trace and the trace ids of affected runs.
Runs execute concurrently, so log lines of different runs are INTERLEAVED in the log.
Every log line has trace=<id>; all lines of one run share the same trace id.

Work step by step:
1. Read the code at the failing location with read_file (never guess code you have not read).
2. You MUST call get_trace for the affected trace ids (at least two) to see the data each run
   actually processed. The stack trace alone does not show the values.
3. If possible, compare with a successful run to confirm the hypothesis
   (find it with search_logs, then get_trace). If none is found after 2 searches, skip this step.
4. Explain the chain: data -> code path -> failure.

Final answer (concise, plain text):
- WHAT HAPPENED
- ROOT CAUSE HYPOTHESIS
- EVIDENCE (quote file:line, trace ids and their log lines)
- SUGGESTED FIX (short code snippet)"""

investigator = create_agent(
    model=llm,
    tools=[list_files, read_file, search_code, get_trace, search_logs],
    system_prompt=INVESTIGATOR_PROMPT,
)


def investigate(inc: Incident) -> str:
    frames = "\n".join(f"  {file}:{line} in {func}()" for file, line, func in inc.frames) or "  (no project frames)"
    task = (
        f"Incident: {inc.exc_type}: {inc.exc_message}\n"
        f"Occurrences: {len(inc.records)}\n"
        f"Affected trace ids: {', '.join(inc.trace_ids[:5]) or 'none (the log lines carry no trace id)'}\n"
        f"Our stack frames (outermost first):\n{frames}"
    )
    messages = [{"role": "user", "content": task}]
    called: set[str] = set()
    findings = ""
    for _ in range(2):  # second attempt only if the agent did not look at any trace
        try:
            for chunk in investigator.stream(
                    {"messages": messages},
                    config={"recursion_limit": 25},
                    stream_mode="updates",
            ):
                for update in chunk.values():
                    for msg in (update or {}).get("messages", []):
                        messages.append(msg)
                        if msg.type == "ai" and msg.tool_calls:
                            for tc in msg.tool_calls:
                                called.add(tc["name"])
                                print(f"  \033[33m→ {tc['name']}({tc['args']})\033[0m")
                        elif msg.type == "tool":
                            preview = str(msg.content).splitlines()[:4]
                            print("\033[2m" + "\n".join(f"    {line}" for line in preview) + "\n    ...\033[0m")
                        elif msg.type == "ai" and msg.content:
                            findings = msg.content
        except GraphRecursionError:
            # step limit hit: keep what was collected and ask for a summary without tools
            print("  \033[31m! step limit reached, summarizing collected evidence\033[0m")
            history = messages[:-1] if getattr(messages[-1], "tool_calls", None) else messages
            findings = llm.invoke([
                {"role": "system", "content": INVESTIGATOR_PROMPT},
                *history,
                {"role": "user", "content": "Step limit reached, no more tool calls. "
                                            "Give your final answer now, based only on what you have already found."},
            ]).content
        if "get_trace" in called or not inc.trace_ids:
            break
        print("  \033[31m! agent skipped get_trace, asking again\033[0m")
        messages.append({"role": "user", "content": (
            "You concluded without looking at the logs of the affected runs. Call get_trace for "
            f"{inc.trace_ids[0]} and other affected traces, check your hypothesis and answer again."
        )})
    return findings


# ───────────────────────────── 4. REPORT ─────────────────────────────
# Facts (how many, when, where, who) come from code. Hypothesis and fix come from the LLM.

REPORT_TEMPLATE = Environment(
    loader=FileSystemLoader(Path(__file__).parent), trim_blocks=True, keep_trailing_newline=True,
).get_template("report.md.j2")


LIST_BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")


def flat_items(items: list[str]) -> list[str]:
    """
    The template puts "- " before every item: drop bullets the model added itself, code fences and empty lines,
    so nested lists and code blocks do not break the Markdown.
    """
    lines = (LIST_BULLET.sub("", line).strip() for item in items for line in item.splitlines())
    return [line for line in lines if line and not line.startswith("```")]


def report(inc: Incident, triage: Triage, owner: str, findings: str, started: float, log_file: Path) -> Path:
    llm_report: Report = llm.with_structured_output(Report, method="json_schema").invoke(
        "Turn these investigation notes into an incident report. Use only facts from the notes.\n\n"
        f"Incident: {inc.exc_type}: {inc.exc_message} at {inc.location}\n\n"
        f"Notes:\n{findings}"
    )
    llm_report.evidence = flat_items(llm_report.evidence)
    llm_report.next_steps = flat_items(llm_report.next_steps)
    md = REPORT_TEMPLATE.render(inc=inc, triage=triage, owner=owner, report=llm_report, findings=findings,
                                model=llm.model, duration=time.perf_counter() - started,
                                log_file=log_file.relative_to(ROOT))
    reports_dir = REPORTS_DIR / log_file.stem  # reports/chain-divide/ for logs/chain-divide.log
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{inc.exc_type}.md"
    path.write_text(md)
    print(md)
    return path


# ───────────────────────────── PIPELINE ──────────────────────────────

def handle(inc: Incident, log_file: Path):
    started = time.perf_counter()
    step("DETECT", f"{inc.exc_type}: {inc.exc_message} @ {inc.location} (x{len(inc.records)})")

    triage, owner = route(inc)
    step("ROUTE", f"{triage.category} / {triage.severity} → {owner}")
    print(f"  {triage.reasoning}")

    if triage.category == "infrastructure":
        findings = "Infrastructure issue, code investigation skipped. See log excerpt:\n" + inc.records[0].text
    else:
        step("INVESTIGATE")
        findings = investigate(inc)

    step("REPORT")
    path = report(inc, triage, owner, findings, started, log_file)
    print(f"\033[32m✔ saved {path.relative_to(ROOT)} in {time.perf_counter() - started:.1f}s\033[0m")


def main():
    started = time.perf_counter()
    total = 0
    for log_file in sorted(LOG_DIR.glob("*.log")):
        step("LOG", str(log_file.relative_to(ROOT)))
        use_log(log_file)  # the agent tools read this file
        incidents = detect(parse_log())
        if not incidents:
            print(f"No errors found in {log_file.relative_to(ROOT)}")
        for inc in incidents:
            handle(inc, log_file)
        total += len(incidents)
    print(f"\nDone: {total} incident(s) in {time.perf_counter() - started:.1f}s with {llm.model}")


if __name__ == "__main__":
    main()
