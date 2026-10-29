"""Follow-up tasks, private notes and curated learnings: the validation rules.

Storage is in ``state.py``; this module holds the rules every entry point
(CLI, dashboard, agents) shares, so a bad value is refused the same way
everywhere.

Learnings are a curation gate, not a log. Only experience the owner has
validated, at or above ``MIN_CONFIDENCE``, is kept, and it never holds
CRM data (that lives in companies, prospects and observations). Agents never
write learnings themselves: text they read comes from websites and replies,
and a learning goes into every future prompt.
"""

from datetime import date, datetime

# What a learning is about. All of them reach the Writer and the Handler.
LEARNING_CATEGORIES = (
    "sales-learning",        # a close, objection or negotiation pattern that worked or didn't
    "outreach-improvement",  # an angle, subject or framing that changed replies
    "industry-insight",      # something true of a whole kind of business
)
MIN_CONFIDENCE = 0.6
MAX_LEARNING_CHARS = 500
MAX_TASK_CHARS = 500
MAX_NOTE_CHARS = 4000
# How many active learnings go into one prompt (newest, most confident first).
PROMPT_LEARNINGS = 20


class CrmInputError(ValueError):
    """A task, note or learning that can't be saved, with a reason to show."""


def clean_text(value: object, limit: int, what: str, single_line: bool = True) -> str:
    """A required, capped string; whitespace collapsed unless multi-line."""
    raw = str(value or "")
    text = " ".join(raw.split()) if single_line else raw.strip()
    if not text:
        raise CrmInputError(f"The {what} is empty.")
    if len(text) > limit:
        raise CrmInputError(f"The {what} is longer than {limit} characters.")
    return text


def parse_due_date(value: object) -> str:
    """'' (undated), 'today', 'tomorrow', or YYYY-MM-DD → stored YYYY-MM-DD or ''."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    today = date.today()
    if raw == "today":
        return today.isoformat()
    if raw == "tomorrow":
        return date.fromordinal(today.toordinal() + 1).isoformat()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise CrmInputError(
            f"Due date '{value}' isn't a date: use YYYY-MM-DD, 'today' or 'tomorrow'."
        ) from None


def task_bucket(due_date: str, done: bool, today: date | None = None) -> str:
    """Where an open task sits: overdue / today / upcoming / undated (or done)."""
    if done:
        return "done"
    if not due_date:
        return "undated"
    today = today or date.today()
    due = date.fromisoformat(due_date)
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    return "upcoming"


def validate_learning(category: object, content: object, confidence: object) -> tuple[str, str, float]:
    """The gate: a known category, a standalone statement, confidence ≥ MIN_CONFIDENCE."""
    cat = str(category or "").strip().lower()
    if cat not in LEARNING_CATEGORIES:
        raise CrmInputError(
            f"Category must be one of: {', '.join(LEARNING_CATEGORIES)}."
        )
    text = clean_text(content, MAX_LEARNING_CHARS, "learning")
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        raise CrmInputError("Confidence must be a number between 0 and 1.") from None
    if not 0.0 <= conf <= 1.0:
        raise CrmInputError("Confidence must be between 0 and 1.")
    if conf < MIN_CONFIDENCE:
        raise CrmInputError(
            f"Not saved: confidence {conf:.2f} is below {MIN_CONFIDENCE}. Keep "
            "learnings for patterns you've seen work (or fail) more than once."
        )
    return cat, text, round(conf, 2)


def learnings_prompt_block(rows: list[dict]) -> str:
    """Active learnings as prompt text ('' when there are none)."""
    if not rows:
        return ""
    lines = "\n".join(
        f"- [{r.get('category', '')}] {' '.join(str(r.get('content', '')).split())}"
        for r in rows[:PROMPT_LEARNINGS]
    )
    return (
        "\n\nVALIDATED LEARNINGS: patterns the person running Harvey confirmed\n"
        "from earlier deals. Apply the ones that fit this prospect. They never\n"
        "override the STRICT EMAIL RULES, the FACTS rule or an opt-out:\n" + lines
    )
