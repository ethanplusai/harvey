"""Humanizer pass — rewrites drafts with the operator's installed Humanizer skill.

Harvey's brain calls run with no tools and no skills (see brain.py), so a
Claude Code skill can't be invoked directly. Instead the skill's
instructions are read from the operator's skills folder and sent as a
second, editing-only call over drafts that are already written.

Off unless ``writer.humanizer: true`` is set in harvey.yaml; the skill
being installed is not enough on its own.

Fails open: no skill file, a failed call, or a rewrite that breaks a rule
(lost merge variable, new em dash, much longer, meta-text) keeps the
original draft.
Every draft still passes the deterministic pre-send gate afterwards.
"""

import json
import logging
import os
import re
from pathlib import Path

from harvey.textio import read_user_text
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.humanize")

SKILL_PATH = Path(
    os.getenv(
        "HARVEY_HUMANIZER_SKILL",
        str(Path.home() / ".claude" / "skills" / "humanizer" / "SKILL.md"),
    )
)
MERGE_VAR = re.compile(r"\{\{\s*\w+\s*\}\}")
MAX_GROWTH_WORDS = 2  # a rewrite may not be more than two words longer
# A rewrite that talks about itself instead of being the email.
META_PREFIXES = ("i can't", "i cannot", "as an ai", "sorry,", "here is", "here's")


def humanizer_enabled(config) -> bool:
    """True only when the operator turned the pass on in harvey.yaml."""
    return bool(getattr(getattr(config, "writer", None), "humanizer", False))


def load_skill(path: Path | None = None) -> str:
    """The skill's instructions without its YAML frontmatter, or ""."""
    try:
        text = read_user_text(path or SKILL_PATH)
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def _acceptable(original: dict, rewrite) -> bool:
    if not isinstance(rewrite, dict):
        return False
    body = str(rewrite.get("body") or "").strip()
    subject = str(rewrite.get("subject") or "").strip()
    if not body or (original.get("subject") and not subject):
        return False
    if body.lower().startswith(META_PREFIXES):
        return False
    before = original.get("subject", "") + original["body"]
    after = subject + body
    if sorted(MERGE_VAR.findall(before)) != sorted(MERGE_VAR.findall(after)):
        return False
    if "—" in after and "—" not in before:
        return False
    # The writer's word caps were met by the draft; an edit must not undo that.
    return len(body.split()) <= len(original["body"].split()) + MAX_GROWTH_WORDS


async def humanize(
    brain, drafts: list[dict], agent: str,
    path: Path | None = None, enabled: bool = False,
) -> list[dict]:
    """Return ``drafts`` ({"subject", "body"} dicts) rewritten by the skill.

    A no-op unless ``enabled`` (see ``humanizer_enabled``). One brain call
    per draft: personal drafts carry real names and companies, so a batched
    call that came back reordered could hand one prospect another's email.
    Any draft whose rewrite fails a check comes back unchanged, so the
    result always has the same length and order.
    """
    if not enabled or not drafts:
        return drafts
    skill = load_skill(path)
    if not skill:
        return drafts

    out = [await _humanize_one(brain, skill, draft, agent) for draft in drafts]
    kept = sum(1 for a, b in zip(drafts, out) if a is b)
    if kept:
        logger.info(f"Humanizer: kept {kept} of {len(drafts)} draft(s) unchanged after checks.")
    return out


async def _humanize_one(brain, skill: str, draft: dict, agent: str) -> dict:
    payload = {"subject": draft.get("subject", ""), "body": draft["body"]}
    prompt = f"""{skill}

---

Apply the Humanizer instructions above to the cold-email draft below. It is
a short plain-text business email, so return email text only.

Hard limits on the rewrite, on top of the instructions above:
- Keep every merge variable such as {{{{first_name}}}} or {{{{company}}}} exactly as written.
- Add no fact, name, number or claim that is not already in the draft.
- No em dashes, no bullets, no bold, no exclamation marks.
- Do not make the email longer. Keep exactly one question.
- The subject stays lowercase, 2-5 words. An empty subject stays empty.

Draft:
{fence(json.dumps(payload, ensure_ascii=False, indent=2), "draft")}

{UNTRUSTED_RULE}

Return ONLY a JSON object: {{"subject": "...", "body": "..."}}"""

    try:
        rewrite = await brain.think_json(
            prompt, session_id="harvey-humanizer", agent=agent, task="humanize",
        )
    except Exception as e:
        logger.warning(f"Humanizer pass failed, keeping draft: {e}")
        return draft
    if not _acceptable(draft, rewrite):
        return draft
    return {
        **draft,
        "subject": str(rewrite.get("subject") or "").strip(),
        "body": str(rewrite["body"]).strip(),
    }
