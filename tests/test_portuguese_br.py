"""Brazilian Portuguese: opt-outs, the opt-out line, persona.language and the
writing checks.

Every name, company and address here is invented.
"""

from types import SimpleNamespace

import pydantic
import pytest

from sellbird.agents.handler import OurText, _opt_out_reason
from sellbird.agents.writer import Writer
from sellbird.config import (
    DEFAULT_OPT_OUT_LINE, PORTUGUESE_DEFAULT_OPT_OUT_LINE, PORTUGUESE_OPT_OUT_WORDS,
    ComplianceConfig, PersonaConfig, SellbirdConfig,
)
from sellbird.email_style import style_problems, with_sign_off
from sellbird.gate import pre_send_check
from sellbird.languages import (
    LANGUAGES, accept_language, canonical_language, language_block, reply_language_block,
)
from tests.test_compliance_footer import _reply
from tests.test_outbox_native import state  # noqa: F401  (state is a fixture)
from tests.test_writer_require_tip import add, config as writer_config
from tests.test_writer_retry import GOOD


# ── Opt-outs ──

@pytest.mark.parametrize("reply", [
    "Não, obrigado.", "não obrigada", "Não, muito obrigado", "Nao, obrigado",
    "SAIR", "Sair, obrigado", "Descadastrar", "Descadastre-me", "descadastre-me, por favor",
    "Quero me descadastrar.", "Como faço para me descadastrar?", "Por favor, me descadastre",
    "Me tira da lista.", "Me tirem da lista de vocês", "Me remova da sua lista, por favor.",
    "Favor me remover do mailing", "Remova-me da lista.", "Tire-me daqui",
    "Remova meu e-mail da sua lista.", "Exclua meus dados.", "Apaguem meus dados, por favor",
    "Solicito a exclusão dos meus dados.", "Quero sair da lista",
    "Gostaria de sair da sua lista de e-mails", "Cancelar minha inscrição",
    "Não me escreva mais.", "Não me mande mais e-mails.", "Não me envie mais nada",
    "Não nos procurem mais", "Por favor, não me contate novamente.", "Não entre mais em contato.",
    "Não entrem em contato comigo.", "Pare de me mandar e-mails.", "Parem de me escrever",
    "Para de me mandar mensagem", "Deixem de enviar e-mails", "Me deixe em paz.",
    "Chega de e-mails.", "Não quero receber mais e-mails.", "Não quero mais receber.",
    "Não queremos receber suas mensagens", "Não quero mais ser contatado.",
    "Não tenho interesse.", "Não temos interesse, obrigado.", "Não me interessa.",
    "Não nos interessa no momento.", "Não estou interessado", "Não estamos interessadas.",
    "Sem interesse.", "Obrigado, mas não temos interesse.", "Já não temos interesse.",
    "Não me manda mais e-mail", "Bom dia, não me manda mais e-mail",
    "Nao tenho interesse", "nao me escreva mais",
    "Não tenho interesse\n\nAtt.\nJoão Exemplo\nTel. (11) 5555-0100",
    "Não temos interesse\nEnviado do meu iPhone",
    "Me tira da lista\n--\nAna Exemplo\nWhatsApp (11) 5555-0100",
])
def test_portuguese_opt_outs(reply):
    assert _opt_out_reason(reply, reply) is not None


@pytest.mark.parametrize("reply", [
    "Sim ou não, obrigado", "Ainda não, obrigado", "Se não, obrigado mesmo assim",
    "Não, obrigado pelo contato. Mas quanto custa?",
    "Me tira uma dúvida: quanto custa?", "Me tira do sério essa demora no orçamento",
    "Não tenho interesse no plano anual, mas no mensal sim.",
    "Não tenho interesse agora, me procure em janeiro.",
    "Não me interessa, mas meu sócio pode se interessar.",
    "Se não me interessar, posso cancelar?", "Você não está interessado? Tudo bem.",
    "Não estou interessado em pagar adiantado, mas podemos conversar.",
    "Não me escreva até janeiro.", "Não me escreva por aqui, me chame no WhatsApp.",
    "Não me envie a proposta por e-mail, me ligue.", "Não me envie mais de duas opções.",
    "Pedro não me manda mais e-mail desde maio.", "Ele me removeu da lista sem avisar",
    "Você me tirou da lista?", "Não quero me descadastrar.", "Não me descadastre, por favor",
    "Não precisa me tirar da lista", "Quero descadastrar nosso fornecedor antigo e contratar vocês.",
    "Pode remover meu sócio da cópia?", "Me remova da cópia, quem cuida disso é o João.",
    "Parem de enviar a nota fiscal em duplicidade.", "Não quero receber boleto em papel.",
    "Quero sair da reunião mais cedo", "Tenho interesse, quanto custa o plano anual?",
])
def test_portuguese_that_is_not_an_opt_out(reply):
    assert _opt_out_reason(reply, reply) is None


def test_every_reply_a_portuguese_opt_out_line_can_ask_for_is_caught():
    for word in PORTUGUESE_OPT_OUT_WORDS:
        assert _opt_out_reason(word, word), word


def test_a_bare_portuguese_opt_out_under_a_mobile_signature():
    reply = "SAIR\n\nEnviado do meu iPhone"
    assert _opt_out_reason(reply, reply)["rule"] == "only_words"
    reply = "Remover\n\nObter o Outlook para Android"
    assert _opt_out_reason(reply, reply)["rule"] == "only_words"


def test_a_long_portuguese_reply_is_quick():
    import time
    text = "nao me tira " * 20000
    started = time.monotonic()
    _opt_out_reason(text, text)
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_a_portuguese_opt_out_reaches_the_handler(state):
    status, _ = await _reply(state, "Não, obrigado.")
    assert status == "opted_out"


def test_our_own_portuguese_footer_quoted_back_is_not_an_opt_out():
    line = PORTUGUESE_DEFAULT_OPT_OUT_LINE
    ours = OurText([line])
    their = ours.remove(f"Tenho interesse, quanto custa?\n> {line}")
    assert _opt_out_reason(their, "Tenho interesse, quanto custa?") is None


# ── The opt-out line ──

def test_portuguese_opt_out_lines_are_accepted():
    for line in (PORTUGUESE_DEFAULT_OPT_OUT_LINE,
                 "Nao tem interesse? E so me avisar que nao envio mais e-mails.",
                 "Responda SAIR para não receber mais mensagens.",
                 'Se não fizer sentido, responda "não, obrigado" e eu paro de escrever.',
                 "Prefere não receber mais? Responda descadastrar.",
                 "Se não for para você, me avise e eu tiro você da minha lista.",
                 "Não quer mais receber meus e-mails? É só responder.",
                 "Sem interesse? Me avise e não escrevo mais."):
        assert ComplianceConfig(opt_out_line=line).opt_out_line == line


def test_portuguese_lines_that_dont_say_it_stops_or_have_a_link_are_refused():
    for bad in ("Obrigado pela atenção!", "Tenha um ótimo dia.",
                "Clique aqui para se descadastrar.",
                "Para se descadastrar: https://example.com/u"):
        with pytest.raises(pydantic.ValidationError):
            ComplianceConfig(opt_out_line=bad)


# ── persona.language ──

def _config(language="", compliance=None):
    data = dict(
        persona=dict(name="Ana Exemplo", company="Clínica Exemplo", role="Sócia", email="ana@example.com",
                     linkedin="", tone="direta", language=language),
        product=dict(name="P", description="D", pricing="", key_benefits=[], objection_responses={}),
        icp=dict(industries=[], company_size="", titles=[], geography=[]),
    )
    if compliance is not None:
        data["compliance"] = compliance
    return SellbirdConfig(**data)


@pytest.mark.parametrize("typed", ["pt-BR", "pt_br", "PT-BR", "pt", "Português",
                                   "português do Brasil", "Brazilian Portuguese"])
def test_the_ways_people_name_brazilian_portuguese(typed):
    assert canonical_language(typed) == "pt-BR"
    assert PersonaConfig(name="A", company="B", role="C", email="a@example.com", linkedin="",
                         tone="t", language=typed).language == "pt-BR"


def test_language_is_off_by_default_and_other_languages_are_kept_as_written():
    assert _config().persona.language == ""
    assert canonical_language(" es ") == "es"
    assert canonical_language(None) == ""


def test_portuguese_brings_its_opt_out_line():
    assert _config().compliance.opt_out_line == DEFAULT_OPT_OUT_LINE
    assert _config("pt-BR").compliance.opt_out_line == PORTUGUESE_DEFAULT_OPT_OUT_LINE
    assert LANGUAGES["pt-BR"].opt_out_line == PORTUGUESE_DEFAULT_OPT_OUT_LINE


def test_an_opt_out_line_the_operator_wrote_is_kept():
    own = "Responda SAIR para não receber mais mensagens."
    assert _config("pt-BR", {"opt_out_line": own}).compliance.opt_out_line == own
    assert _config("pt-BR", {"opt_out_line": ""}).compliance.opt_out_line == ""
    explicit_default = {"opt_out_line": DEFAULT_OPT_OUT_LINE}
    assert _config("pt-BR", explicit_default).compliance.opt_out_line == DEFAULT_OPT_OUT_LINE


def test_the_language_block():
    assert language_block(_config()) == ""
    assert reply_language_block(_config()) == ""
    block = language_block(_config("pt-BR"))
    assert "Brazilian Portuguese (pt-BR)" in block and '"você"' in block
    assert "in es" in language_block(_config("es"))
    assert "language the prospect wrote in" in reply_language_block(_config("pt-BR"))


def test_scout_asks_for_portuguese_pages():
    assert accept_language(_config("pt-BR"), "en-US").startswith("pt-BR")
    assert accept_language(_config(), "en-US") == "en-US"


class _CapturingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return GOOD


@pytest.mark.asyncio
async def test_the_writer_is_told_the_language(state):
    await add(state, 1, notes="Abriu uma segunda unidade.")
    brain = _CapturingBrain()
    config = writer_config()
    config.persona = SimpleNamespace(name="S", company="C", role="R", tone="t", language="pt-BR")
    await Writer(brain, state, config, None).run()
    assert brain.prompts and all("Brazilian Portuguese" in p for p in brain.prompts)


@pytest.mark.asyncio
async def test_without_a_language_the_writer_prompt_is_unchanged(state):
    await add(state, 1, notes="Opened a second shop.")
    brain = _CapturingBrain()
    await Writer(brain, state, writer_config(), None).run()
    assert brain.prompts and not any("LANGUAGE:" in p for p in brain.prompts)


# ── Writing checks ──

def test_portuguese_ai_tells_are_caught():
    found = style_problems("sua agenda", "Olá, Ana,\n\nEspero que esteja bem. Vi que a agenda fecha cedo?")
    assert any("espero que esteja bem" in p for p in found)
    found = style_problems("sua agenda", "Oi, Ana,\n\nNão só agenda, mas também lembretes. Quer ver?")
    assert any("not just X but Y" in p for p in found)
    assert not style_problems("agenda online", "Oi, Ana,\n\nVi que a agenda só aceita telefone. "
                                                "Posso mandar um exemplo de agenda online?")


def test_a_portuguese_greeting_is_skipped_and_a_self_opening_caught():
    assert any('opens with "I" or "we"' in p
               for p in style_problems("sua agenda", "Olá, Ana,\n\nEu ajudo clínicas a lotar a agenda. Faz sentido?"))
    assert any('opens with "I" or "we"' in p
               for p in style_problems("sua agenda", "Nossa empresa ajuda clínicas. Faz sentido?"))
    assert not any('opens with' in p
                   for p in style_problems("sua agenda", "Bom dia, Ana,\n\nVi que vocês atendem sábado. Faz sentido?"))


@pytest.mark.parametrize("sign_off", ["Abraços,", "Att.", "Atenciosamente,", "Um abraço,", "Obrigado,"])
def test_a_portuguese_sign_off_is_replaced_by_the_name(sign_off):
    body = f"Vi que a agenda fecha cedo. Quer um exemplo?\n\n{sign_off}\nAna"
    assert with_sign_off(body, "Ana Exemplo") == "Vi que a agenda fecha cedo. Quer um exemplo?\n\nAna"


def test_portuguese_spam_phrases_are_held_at_the_gate():
    result = pre_send_check("ana@example.com", "sua agenda", "Clique aqui e veja. Quer ver?", kind="reply")
    assert not result.ok and any("clique aqui" in r for r in result.reasons)
    assert pre_send_check("ana@example.com", "sua agenda", "Vi a agenda de vocês. Quer ver?",
                          kind="reply").ok
