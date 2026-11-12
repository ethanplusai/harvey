"""The eval loop's scoreboard: what Sellbird sent, what came back, and what
reviewers had to fix.

Counted per company, because a reply from anyone at a company stops the
sequence to everyone there. Companies are grouped by the ICP industry that
found them, so a raw search phrase rolls up into the industry it came from.
"""

from __future__ import annotations

import difflib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import aiosqlite

# Strongest first: a company's outcome is its strongest intent.
INTENT_RANK = ["interested", "question", "objection", "wrong_person",
               "not_interested", "unsubscribe", "escalate"]
POSITIVE_INTENTS = {"interested", "question"}
MEETING_STAGES = {"closing", "closed_won"}
NOT_A_REPLY = {"ooo"}


def segment_for(found_by: str, config) -> str:
    """The ICP industry a company was found under, or "other"."""
    text = str(found_by or "").strip().lower()
    if not text:
        return "other"
    icp = getattr(config, "icp", None)
    industries = list(getattr(icp, "industries", []) or [])
    variants = dict(getattr(icp, "keyword_variants", {}) or {})
    for industry in industries:
        if text == industry.lower():
            return industry
    for industry, words in variants.items():
        if any(text == w.lower() for w in words):
            return industry
    for industry in sorted(industries, key=len, reverse=True):
        if industry.lower() in text:
            return industry
    for industry, words in variants.items():
        if any(w.lower() in text for w in words):
            return industry
    return "other"


def _rate(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


async def _rows(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def scoreboard(db_path: str, config, since: str) -> dict:
    """Outcomes per segment and per sequence step since ``since`` (ISO)."""
    sent = await _rows(db_path, """
        SELECT o.prospect_id, o.step, o.kind, o.sent_at, p.company_id,
               p.email_status, c.industry AS found_by
        FROM outbox o JOIN prospects p ON p.id = o.prospect_id
        LEFT JOIN companies c ON c.id = p.company_id
        WHERE o.status = 'sent' AND o.kind = 'sequence' AND COALESCE(p.company_id, '') != ''
          AND datetime(o.sent_at) >= datetime(?)
        ORDER BY o.sent_at""", (since,))
    convos = await _rows(db_path, """
        SELECT cv.intent, cv.stage, cv.created_at, p.company_id
        FROM conversations cv JOIN prospects p ON p.id = cv.prospect_id
        WHERE p.company_id != ''""")

    companies: dict[str, dict] = {}
    step_sent: Counter = Counter()
    for row in sent:
        step_sent[int(row["step"])] += 1
        co = companies.setdefault(row["company_id"], {
            "segment": segment_for(row["found_by"], config), "first_sent": row["sent_at"],
            "sends": [], "bounced": False})
        co["sends"].append((row["sent_at"], int(row["step"])))
        if row["email_status"] == "invalid":
            co["bounced"] = True

    for co_id, co in companies.items():
        replies = [c for c in convos if c["company_id"] == co_id
                   and c["intent"] not in NOT_A_REPLY
                   and str(c["created_at"]) >= str(co["first_sent"])]
        co["intent"] = min((c["intent"] for c in replies),
                           key=lambda i: INTENT_RANK.index(i) if i in INTENT_RANK else 99,
                           default="")
        co["meeting"] = any(c["stage"] in MEETING_STAGES for c in replies)
        first_reply = min((str(c["created_at"]) for c in replies), default="")
        before = [s for at, s in co["sends"] if first_reply and str(at) <= first_reply]
        co["replied_after_step"] = max(before) if before else None

    def tally(group: list[dict]) -> dict:
        n = len(group)
        delivered = n - sum(c["bounced"] for c in group)
        replied = sum(bool(c["intent"]) for c in group)
        return {
            "companies": n, "bounced": n - delivered, "replied": replied,
            "positive": sum(c["intent"] in POSITIVE_INTENTS for c in group),
            "meetings": sum(c["meeting"] for c in group),
            "reply_rate": _rate(replied, delivered),
        }

    by_segment = defaultdict(list)
    for co in companies.values():
        by_segment[co["segment"]].append(co)
    steps = {}
    for step in sorted(step_sent):
        replied = sum(1 for c in companies.values() if c["replied_after_step"] == step)
        steps[step] = {"sent": step_sent[step], "replies_after": replied}

    return {
        "since": since,
        "total": tally(list(companies.values())),
        "segments": {seg: tally(group) for seg, group in
                     sorted(by_segment.items(), key=lambda kv: -len(kv[1]))},
        "steps": steps,
    }


def summarize_edits(revisions: list[dict]) -> dict:
    """How often reviewers rewrote drafts, by kind of email."""
    by_kind = Counter(r.get("kind") or "?" for r in revisions)
    return {"edits": len(revisions), "drafts_edited": len({r["outbox_id"] for r in revisions}),
            "by_kind": dict(by_kind)}


def edit_diff(revision: dict, context: int = 0) -> str:
    """The lines a reviewer changed, as a compact unified diff."""
    old = (revision.get("old_subject", "") + "\n" + revision.get("old_body", "")).splitlines()
    new = (revision.get("new_subject", "") + "\n" + revision.get("new_body", "")).splitlines()
    lines = difflib.unified_diff(old, new, lineterm="", n=context)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++", "@@")))


def since_days(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat()


def as_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)
