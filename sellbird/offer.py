"""Design the first-email offer with Sellbird (the dashboard's "Your offer").

Every first email ends by offering ``product.offer.entry``: something free and
useful the sender can really deliver. The offer is the sender's call, so
Sellbird helps design it instead of picking it alone:

- ``suggest_offers`` asks the brain for a few offers that fit the product,
  each paired with the finding about a prospect that makes it the obvious
  next step.
- ``preview_email`` writes one real first email with a draft offer, for a real
  prospect, without saving or sending anything.
- ``save_offer`` writes the chosen offer to the config file, keeping a backup.
"""

import copy
import json
import logging
from pathlib import Path

import yaml

from sellbird.textio import read_user_text
from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.offer")

MAX_OFFER_CHARS = 300
SUGGESTIONS = 4

SUGGEST_PROMPT = """You help a business design the offer at the end of its cold emails.

The business:
{product}

Who it emails: {icp}

A first email names one finding about the prospect (where they rank on Google,
what their website lacks, a role they're hiring for, their reviews), says what
that costs them, and ends by offering one free, useful thing as the next step to
fix it. Suggest {n} offers for this business. Each must be:
- tied to a finding Sellbird can really see about a prospect (search rank, the
  website, job posts, reviews, tools on the site, funding news);
- specific and valuable on its own, so saying yes costs nothing ("a written plan
  for getting you onto page one", "a mockup of your homepage with online
  booking", "a two-week trial answering your after-hours calls");
- something this business can deliver quickly after a yes;
- never a discount, a generic call or a demo.

{rule}

Return ONLY a JSON array of {n} objects:
[{{"offer": "the offer in a few words, as it would be written in the email",
   "finding": "the kind of finding it pairs with",
   "why": "one sentence on why a prospect would say yes"}}]"""


def offer_view(config) -> dict:
    """What the dashboard shows: the offer, the ideas, and the product it's for."""
    offer = config.product.offer
    return {
        "entry": offer.entry or "",
        "entry_ideas": list(getattr(offer, "entry_ideas", []) or []),
        "product": {"name": config.product.name, "description": config.product.description},
    }


def _product_text(config) -> str:
    p = config.product
    lines = [f"Name: {p.name}", f"What it is: {p.description}", f"Pricing: {p.pricing}"]
    lines += [f"Benefit: {b}" for b in p.key_benefits or []]
    return "\n".join(lines)


def _icp_text(config) -> str:
    icp = config.icp
    parts = []
    if icp.titles:
        parts.append(", ".join(icp.titles))
    if icp.industries:
        parts.append("in " + ", ".join(icp.industries))
    if icp.company_size:
        parts.append(f"({icp.company_size})")
    return " ".join(parts) or "not set"


async def suggest_offers(brain, config) -> list[dict]:
    """A few offers that fit the product, each with its finding and why."""
    prompt = SUGGEST_PROMPT.format(
        product=fence(_product_text(config), "product info"), icp=_icp_text(config),
        n=SUGGESTIONS, rule=UNTRUSTED_RULE)
    result = await brain.think_json(prompt, session_id="sellbird-offer", agent="writer",
                                    task="suggest_offers")
    if isinstance(result, dict):
        result = next((v for v in result.values() if isinstance(v, list)), [])
    out = []
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict):
            continue
        offer = str(item.get("offer") or "").strip()[:MAX_OFFER_CHARS]
        if offer:
            out.append({"offer": offer, "finding": str(item.get("finding") or "").strip(),
                        "why": str(item.get("why") or "").strip()})
    return out[:SUGGESTIONS]


async def preview_prospect(state):
    """A real prospect with something to write about, or None.

    Prefers one whose company has signals or a description, so the preview
    shows a finding, not a guess."""
    best = None
    for status in ("new", "queued", "contacted"):
        for p in await state.get_prospects_by_status(status):
            if not p.company_id or p.is_company_inbox:
                continue
            company = await state.get_company(p.company_id)
            if company and (company.signals or p.personalization_notes):
                return p
            if best is None and company and company.description:
                best = p
    return best


async def preview_email(brain, state, config, entry: str) -> dict:
    """One first email written with ``entry`` as the offer. Nothing is saved."""
    from sellbird.agents.writer import Writer, _names
    prospect = await preview_prospect(state)
    if prospect is None:
        return {"ok": False, "message": "No prospects with company facts yet. Find some companies "
                                        "first, then preview the offer on one of them."}
    trial = copy.deepcopy(config)
    trial.product.offer.entry = entry
    writer = Writer(brain, state, trial, None)
    writer.skills = brain.load_skills_for_agent("writer")
    writer.email_rules = ""
    draft = await writer._write_personal_email(prospect)
    draft = await writer._keep_to_rules(draft, step=1, names=_names(prospect)) if draft else None
    if not draft:
        return {"ok": False, "message": "The writer couldn't produce an email that passes the rules. "
                                        "Try again, or reword the offer."}
    return {"ok": True, "to": f"{prospect.full_name()}, {prospect.title} at {prospect.company}",
            "subject": draft["subject"], "body": draft["body"]}


def save_offer(config_file: Path, entry: str, ideas: list[str] | None = None) -> Path:
    """Write ``entry`` (and the ideas, when given) to product.offer in the
    config file. Keeps a .bak of the file as it was."""
    entry = " ".join(str(entry or "").split())[:MAX_OFFER_CHARS]
    raw = read_user_text(config_file)
    data = yaml.safe_load(raw) or {}
    product = data.setdefault("product", {})
    offer = product.setdefault("offer", {})
    if not isinstance(offer, dict):
        offer = product["offer"] = {}
    offer["entry"] = entry
    if ideas is not None:
        offer["entry_ideas"] = [" ".join(str(i).split())[:MAX_OFFER_CHARS] for i in ideas if str(i).strip()][:8]
    config_file.with_name(config_file.name + ".bak").write_text(raw, encoding="utf-8")
    config_file.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False,
                                          allow_unicode=True), encoding="utf-8")
    return config_file
