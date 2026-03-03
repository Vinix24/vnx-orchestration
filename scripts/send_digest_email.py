#!/usr/bin/env python3
"""
VNX Nightly Digest — Email Sender.

Reads t0_session_brief.json and pending_edits.json, formats a digest email,
and sends via SMTP (default: Gmail).

Configuration via environment variables:
  VNX_DIGEST_EMAIL  — recipient email address (required)
  VNX_SMTP_PASS     — SMTP password / Gmail App Password (required)
  VNX_SMTP_USER     — SMTP username (defaults to VNX_DIGEST_EMAIL)
  VNX_SMTP_HOST     — SMTP server (default: smtp.gmail.com)
  VNX_SMTP_PORT     — SMTP port (default: 587)

Usage:
  python3 scripts/send_digest_email.py           # send digest
  python3 scripts/send_digest_email.py --dry-run  # print without sending
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_UTC = timezone.utc

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
BRIEF_PATH = STATE_DIR / "t0_session_brief.json"
PENDING_PATH = STATE_DIR / "pending_edits.json"
LOG_PATH = STATE_DIR / "conversation_analyzer.log"


def _load_json(path: Path) -> dict:
    """Load JSON file, return empty dict on failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _format_model_performance(perf: dict) -> str:
    """Format model_performance into readable text."""
    if not perf:
        return "Geen model data beschikbaar.\n"

    lines = []
    for model, data in sorted(perf.items()):
        sessions = data.get("sessions_7d", 0)
        avg_tok = data.get("avg_tokens_per_session", 0)
        avg_k = round(avg_tok / 1000) if avg_tok else 0
        err_rate = data.get("error_recovery_rate", 0)
        cache = data.get("cache_hit_ratio", 0)
        dur = data.get("avg_duration_minutes", 0)

        activities = data.get("primary_activities", {})
        top_activities = sorted(activities.items(), key=lambda x: x[1], reverse=True)[:3]
        act_str = ", ".join(f"{a} ({c})" for a, c in top_activities) if top_activities else "-"

        lines.append(
            f"  {model}: {sessions} sessies | {avg_k}K tok/sess | "
            f"err={err_rate:.0%} | cache={cache:.0%} | {dur:.0f}min\n"
            f"    Top activiteiten: {act_str}"
        )

    return "\n".join(lines) + "\n"


def _format_routing_hints(hints: list) -> str:
    """Format routing hints into readable text."""
    if not hints:
        return "Geen routing hints (onvoldoende data).\n"

    lines = []
    for h in hints:
        task = h.get("task_type", "?")
        model = h.get("recommended_model", "?")
        conf = h.get("confidence", 0)
        evidence = h.get("evidence", "")
        lines.append(f"  {task} → {model} (confidence: {conf:.0%})\n    {evidence}")

    return "\n".join(lines) + "\n"


def _format_concerns(concerns: list) -> str:
    """Format active concerns into readable text."""
    if not concerns:
        return "Geen actieve waarschuwingen.\n"

    lines = []
    for c in concerns:
        model = c.get("model", "?")
        concern = c.get("concern", "")
        rec = c.get("recommendation", "")
        lines.append(f"  ⚠ {model}: {concern}\n    → {rec}")

    return "\n".join(lines) + "\n"


def _format_pending_edits(edits_data: dict) -> str:
    """Format pending edits into readable text."""
    edits = edits_data.get("edits", [])
    pending = [e for e in edits if e.get("status") == "pending"]

    if not pending:
        return "Geen pending suggesties.\n"

    cat_labels = {
        "memory": "MEMORY",
        "rule": "RULE",
        "claude_md": "CLAUDE.MD",
        "skill": "SKILL",
        "hook": "HOOK",
    }

    lines = [f"{len(pending)} pending suggesties:\n"]
    for edit in pending:
        eid = edit.get("id", "?")
        cat = cat_labels.get(edit.get("category", ""), edit.get("category", "").upper())
        target = Path(edit.get("target", "")).name
        content = edit.get("content", "").split("\n")[0][:80]
        confidence = edit.get("confidence", 0)
        evidence = edit.get("evidence", "")
        action = edit.get("action", "append")
        action_label = "Toevoegen" if action in ("append", "append_section") else "Wijzigen"

        lines.append(
            f"  #{eid} [{cat}] {target}\n"
            f"    {action_label}: {content}\n"
            f"    Confidence: {confidence:.2f} | Bewijs: {evidence}"
        )

    lines.append(
        "\nReview: vnx suggest review\n"
        "Accept: vnx suggest accept <ids>\n"
        "Reject: vnx suggest reject <ids>\n"
        "Apply:  vnx suggest apply"
    )

    return "\n".join(lines) + "\n"


def _get_log_tail(max_lines: int = 15) -> str:
    """Get last N lines of the analyzer log."""
    if not LOG_PATH.exists():
        return "Geen analyzer log beschikbaar.\n"
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        return "\n".join(f"  {line}" for line in tail) + "\n"
    except OSError:
        return "Log niet leesbaar.\n"


def build_digest() -> tuple[str, str]:
    """Build the digest email subject and body.

    Returns:
        (subject, body) tuple.
    """
    now = datetime.now(tz=_UTC)
    date_str = now.strftime("%Y-%m-%d")

    brief = _load_json(BRIEF_PATH)
    edits_data = _load_json(PENDING_PATH)

    model_perf = brief.get("model_performance", {})
    hints = brief.get("model_routing_hints", [])
    concerns = brief.get("active_concerns", [])
    volume = brief.get("session_volume", {})
    total_sessions = volume.get("total_7d", 0)
    lookback = brief.get("lookback_days", 7)

    pending_count = len([e for e in edits_data.get("edits", []) if e.get("status") == "pending"])

    # Subject
    model_count = len(model_perf)
    concern_icon = " ⚠" if concerns else ""
    subject = (
        f"VNX Digest {date_str} — "
        f"{total_sessions} sessies, {model_count} models, "
        f"{pending_count} suggesties{concern_icon}"
    )

    # Body
    sections = [
        f"VNX Nightly Digest — {date_str}",
        f"{'=' * 50}",
        "",
        f"Periode: afgelopen {lookback} dagen | {total_sessions} sessies geanalyseerd",
        "",
        "─── Model Performance ───",
        "",
        _format_model_performance(model_perf),
        "─── Routing Hints ───",
        "",
        _format_routing_hints(hints),
        "─── Waarschuwingen ───",
        "",
        _format_concerns(concerns),
        "─── Voorgestelde Wijzigingen ───",
        "",
        _format_pending_edits(edits_data),
        "─── Analyzer Log (laatste regels) ───",
        "",
        _get_log_tail(),
        "─────────────────────────────",
        f"Gegenereerd: {now.isoformat().replace('+00:00', 'Z')}",
        "VNX Orchestration System",
    ]

    body = "\n".join(sections)
    return subject, body


def send_email(subject: str, body: str, dry_run: bool = False) -> bool:
    """Send the digest email via SMTP.

    Returns:
        True if sent successfully, False otherwise.
    """
    recipient = os.environ.get("VNX_DIGEST_EMAIL", "")
    smtp_pass = os.environ.get("VNX_SMTP_PASS", "")
    smtp_user = os.environ.get("VNX_SMTP_USER", "") or recipient
    smtp_host = os.environ.get("VNX_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("VNX_SMTP_PORT", "587"))

    if not recipient:
        print("ERROR: VNX_DIGEST_EMAIL not set", file=sys.stderr)
        return False
    if not smtp_pass and not dry_run:
        print("ERROR: VNX_SMTP_PASS not set", file=sys.stderr)
        return False

    if dry_run:
        print(f"=== DRY RUN ===")
        print(f"To: {recipient}")
        print(f"From: {smtp_user}")
        print(f"Subject: {subject}")
        print(f"Host: {smtp_host}:{smtp_port}")
        print(f"{'=' * 50}")
        print(body)
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"VNX Orchestration <{smtp_user}>"
    msg["To"] = recipient

    # Plain text version
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # HTML version (monospace for alignment)
    html_body = (
        "<html><body>"
        '<pre style="font-family: Menlo, Monaco, monospace; font-size: 13px; '
        'line-height: 1.5; color: #333; background: #f8f9fa; padding: 20px; '
        'border-radius: 8px;">'
        f"{body}"
        "</pre></body></html>"
    )
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"Digest sent to {recipient}")
        return True
    except smtplib.SMTPAuthenticationError:
        print(
            "ERROR: SMTP authentication failed. Check VNX_SMTP_PASS (Gmail requires App Password).",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"ERROR: Failed to send email: {exc}", file=sys.stderr)
        return False


def main():
    dry_run = "--dry-run" in sys.argv

    subject, body = build_digest()
    success = send_email(subject, body, dry_run=dry_run)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
