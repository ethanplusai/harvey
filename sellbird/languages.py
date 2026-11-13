"""The language Sellbird writes cold email in (persona.language).

Unset, nothing changes: the prompts are English and so is what the model
writes. Set, every email and reply Sellbird drafts is written in that
language, Scout asks prospects' websites for pages in it, and a language
Sellbird knows well brings its own default opt-out line and writing notes.

Any language can be named ("es", "German"); the ones in ``LANGUAGES`` get
the extra care. The checks in sellbird.email_style, sellbird.gate and the
Handler's opt-out rules read every language they know whatever this says,
because a reply comes in whatever language its writer chose.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str               # BCP 47, the canonical form of persona.language
    name: str               # how the prompts name it
    accept_language: str    # what Scout's requests ask websites for
    opt_out_line: str       # compliance.opt_out_line when the operator set none
    writing_notes: str      # what the writer prompts add for it


_PT_BR_NOTES = """\
- Everyday Brazilian business Portuguese, the way a person types to someone
  they haven't met: "você", never "o senhor", "a senhora" or "tu". Correct
  accents always (não, você, é).
- Greeting: "Oi, Sam," or "Olá, Sam,"; "Olá," for a company inbox. Never
  "Prezado", "Prezada", "Caro" or "Cara".
- Subject: lowercase, 2 to 5 words; a name keeps its capital.
- None of these, nor anything like them: "espero que esteja bem", "espero que
  este e-mail o encontre bem", "estou entrando em contato", "venho por meio
  deste", "gostaria de apresentar", "só passando para", "sem compromisso",
  "pergunta rápida", "imperdível", "revolucionar", "alavancar", "potencializar",
  "solução completa", "de ponta", "não é apenas", "parabéns".
- Short words over long ones: "ver" over "visualizar", "usar" over "utilizar"."""

LANGUAGES = {
    "pt-BR": Language(
        code="pt-BR",
        name="Brazilian Portuguese (pt-BR)",
        accept_language="pt-BR,pt;q=0.9,en;q=0.5",
        opt_out_line="Não tem interesse? É só me avisar que não envio mais e-mails.",
        writing_notes=_PT_BR_NOTES,
    ),
}

# What people type for a language Sellbird knows, folded to lower case.
_ALIASES = {
    "pt-br": "pt-BR", "pt_br": "pt-BR", "pt": "pt-BR", "portuguese": "pt-BR",
    "brazilian portuguese": "pt-BR", "portugues": "pt-BR", "português": "pt-BR",
    "portugues do brasil": "pt-BR", "português do brasil": "pt-BR",
    "portugues brasileiro": "pt-BR", "português brasileiro": "pt-BR",
}


def canonical_language(value) -> str:
    """persona.language as Sellbird keeps it: a known language's code
    ("pt-BR" for "pt_br", "Português"), anything else trimmed as written,
    "" for none."""
    raw = " ".join(str(value or "").split())
    return _ALIASES.get(raw.lower(), raw)


def language_of(config) -> str:
    """The configured writing language, or "" when none is set."""
    persona = getattr(config, "persona", None)
    return canonical_language(getattr(persona, "language", "") if persona else "")


def known_language(config) -> Language | None:
    return LANGUAGES.get(language_of(config))


def language_block(config) -> str:
    """The prompt section that sets the language of an email, or "" for none."""
    code = language_of(config)
    if not code:
        return ""
    known = LANGUAGES.get(code)
    name = known.name if known else code
    block = (f"\n\nLANGUAGE: Write every email, subject and body, in {name}, however the\n"
             "facts and the instructions above are written. Every rule above applies in\n"
             "that language too.")
    if known and known.writing_notes:
        block += "\n" + known.writing_notes
    return block


def reply_language_block(config) -> str:
    """The language rule for a reply: theirs first, then the configured one."""
    code = language_of(config)
    if not code:
        return ""
    known = LANGUAGES.get(code)
    name = known.name if known else code
    block = (f"\n\nLANGUAGE: Reply in the language the prospect wrote in. If you can't tell,\n"
             f"write in {name}.")
    if known and known.writing_notes:
        block += f" When you write in {name}:\n" + known.writing_notes
    return block


def write_in(config) -> str:
    """One line asking for text in the configured language, or "" for none."""
    code = language_of(config)
    if not code:
        return ""
    known = LANGUAGES.get(code)
    return f"\nWrite every text value in {known.name if known else code}."


def accept_language(config, default: str) -> str:
    known = known_language(config)
    return known.accept_language if known else default
