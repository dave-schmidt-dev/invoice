"""Local weekly-summary configuration, text cleanup and week grouping for zd (moved verbatim from zd.py)."""

import os
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import click
from cli_logging import default_log_file
from zd_store import to_money, week_label, week_key


LOCAL_SUMMARY_BASE_URL = os.environ.get("ZD_SUMMARY_BASE_URL", "http://127.0.0.1:8086")


LOCAL_SUMMARY_MODEL = os.environ.get("ZD_SUMMARY_MODEL", "summarizer")


LOCAL_SUMMARY_MODEL_PATH = os.environ.get(
    "ZD_SUMMARY_MODEL_PATH",
    str(Path.home() / "models/narrator-bench/gemma-e2b/gemma-4-E2B-it-Q4_K_M.gguf"),
)


LOCAL_SUMMARY_LOG = os.environ.get("ZD_SUMMARY_LOG", default_log_file("zd-summary-server"))


LOCAL_SUMMARY_TIMEOUT = 30.0


LOCAL_SUMMARY_STARTUP_TIMEOUT = 60.0


class WeekSummaryError(Exception):
    """Raised when weekly summary generation cannot produce usable text."""


class SummaryServerError(Exception):
    """Raised when the local llama-server cannot be brought up for summaries."""


def _clean_week_summary(text):
    """Normalize local LLM output into a short invoice-safe phrase."""
    summary = " ".join(str(text or "").strip().split())
    if not summary:
        raise WeekSummaryError("empty summary")
    summary = summary.strip("\"'` ")
    if summary.endswith("."):
        summary = summary[:-1]
    if len(summary) > 140:
        summary = summary[:137].rstrip() + "..."
    return summary


def _notes_for_summary(sessions):
    """Build compact dated notes text for a weekly summary prompt."""
    lines = []
    for s in sessions:
        notes = (s["notes"] or "").strip()
        if notes:
            lines.append(f"- {s['work_date']}: {notes}")
    return "\n".join(lines)


def _summary_timeout(value, default=LOCAL_SUMMARY_TIMEOUT):
    """Return a positive timeout value, falling back for invalid config/env input."""
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    if timeout <= 0:
        return default
    return timeout


def _weekly_summary_config(config):
    """Return normalized weekly summary settings from invoice config.

    Adds `model_path` (path to the GGUF weights) and `log_path` (where
    llama-server's stdout/stderr go when we spawn it) so the auto-start
    helper can find them without extra config plumbing."""
    summary_config = (
        config.get("zd", {})
        .get("weekly_summaries", {})
    )
    default_timeout = _summary_timeout(os.environ.get("ZD_SUMMARY_TIMEOUT"))
    return {
        "enabled": bool(summary_config.get("enabled", False)),
        "base_url": summary_config.get("base_url") or LOCAL_SUMMARY_BASE_URL,
        "model": summary_config.get("model") or LOCAL_SUMMARY_MODEL,
        "model_path": summary_config.get("model_path") or LOCAL_SUMMARY_MODEL_PATH,
        "log_path": summary_config.get("log_path") or LOCAL_SUMMARY_LOG,
        "timeout_seconds": _summary_timeout(summary_config.get("timeout_seconds"), default_timeout),
    }


def _parse_host_port(base_url, default_port=8086):
    parsed = urllib.parse.urlparse(base_url)
    return parsed.hostname or "127.0.0.1", str(parsed.port or default_port)


def _is_loopback_host(host):
    """True if `host` refers to the local machine (INV-1: client session
    notes must not silently leave the machine for summarization).

    Recognizes the common loopback spellings: "127.0.0.1" (and the rest of
    the 127.0.0.0/8 block), "::1", and "localhost". Anything else (a LAN IP,
    a hostname, a public address) is treated as non-loopback."""
    host = (host or "").strip().lower()
    if host in ("localhost", "::1"):
        return True
    return host.startswith("127.")


def summarize_week_with_local_gemma(
    label,
    sessions,
    *,
    base_url=LOCAL_SUMMARY_BASE_URL,
    model=LOCAL_SUMMARY_MODEL,
    timeout=LOCAL_SUMMARY_TIMEOUT,
):
    """Generate a one-line weekly invoice summary using the local Gemma server."""
    notes = _notes_for_summary(sessions)
    if not notes:
        raise WeekSummaryError("no notes to summarize")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write concise professional invoice line item summaries. "
                    "Return exactly one plain-text sentence fragment, no markdown, no quotes, no bullets."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Summarize this consulting work for {label} in 12 words or fewer. "
                    "Do not mention hours, dates, rates, invoices, or the client name.\n\n"
                    f"{notes}"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 48,
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise WeekSummaryError(str(exc)) from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise WeekSummaryError("local summary response missing content") from exc
    return _clean_week_summary(content)


def group_sessions_by_week(sessions, summary_provider=None):
    """
    Group a list of session rows by calendar week.

    Grouped by the Monday's full ISO date (week_key), which is year-inclusive,
    so two sessions whose weeks share a month/day Monday but fall in
    different years are never merged into one line item (a silent billing
    merge across years). The human-facing `label`/description is derived from
    the group's actual session dates (week_label), so it never shows a date
    outside the billed period. Returns list of dicts:
    {description, hours, rate, amount}, sorted chronologically by the Monday
    ISO key.
    """
    weeks = {}
    for s in sessions:
        key = week_key(s["work_date"])
        if key not in weeks:
            weeks[key] = {
                "sessions": [],
                "hours": 0.0,
                "rate": s["rate"],
            }
        weeks[key]["sessions"].append(s)
        weeks[key]["hours"] += s["hours"]

    result = []
    for key in sorted(weeks.keys()):
        data = weeks[key]
        # Label from the dates actually worked, not the enclosing Mon-Sun
        # week, so an invoice scoped to a month never displays a prior-month
        # Monday.
        label = week_label([x["work_date"] for x in data["sessions"]])
        rate = data["rate"]
        # Use the raw accumulated hours — to_money already quantizes the
        # amount to cents (Decimal/ROUND_HALF_UP). A pre-round of hours here
        # is an extra rounding step that only adds skew (INV-4).
        hours = data["hours"]
        amount = float(to_money(hours * rate))
        description = label
        if summary_provider is not None:
            try:
                summary = summary_provider(label, data["sessions"])
                if summary:
                    description = f"{label} - {_clean_week_summary(summary)}"
            except (WeekSummaryError, SummaryServerError) as exc:
                click.echo(f"  ⚠  Weekly summary unavailable for {label}: {exc}")
        result.append({
            "description": description,
            "hours": hours,
            "rate": rate,
            "amount": amount,
        })
    return result
