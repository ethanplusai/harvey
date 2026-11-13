"""Who decides at a Brazilian small business.

Same shapes as test_people_finder.py and test_people_finder_fr.py, in
Brazilian Portuguese: an owner on a card, a sócia named in a sentence, a
founder in "fundada por", a customer quoted in a depoimento. Every business
and person here is fictional.
"""

import pytest

from sellbird.agents.scout import Scout
from sellbird.collectors import people
from sellbird.collectors.discover import OpenStreetMap
from sellbird.collectors.profile import ProfileCollector, title_looks_like_role

detect_people = ProfileCollector(state=None).detect_people

DOMAIN = "clinicaexemplo.test"
SITE = "Clínica Exemplo"


def prose(html):
    return [(p["full_name"], p["title"]) for p in people.people_from_prose(html, DOMAIN, SITE)]


# ── names ──

@pytest.mark.parametrize("text, expected", [
    ("João da Silva", ("João", "da Silva")),
    ("Ana Paula Souza", ("Ana", "Souza")),
    ("João da Silva Santos", ("João", "Santos")),
    ("Maria Fernanda de Oliveira Lima", ("Maria", "Lima")),
    ("Luiz Carlos dos Anjos", ("Luiz", "dos Anjos")),
    ("Dra. Beatriz Monteiro", ("Beatriz", "Monteiro")),
    ("Dr. Otávio Rezende", ("Otávio", "Rezende")),
    ("Sra. Lúcia Prado", ("Lúcia", "Prado")),
    ("Dona Cecília Brandão", ("Cecília", "Brandão")),
    ("Dra. Beatriz Monteiro - CRO-SP 12345", ("Beatriz", "Monteiro")),
    ("Renato Albuquerque, OAB/SP 123.456", ("Renato", "Albuquerque")),
])
def test_brazilian_names_are_names(text, expected):
    assert people.split_person_name(text) == expected


@pytest.mark.parametrize("text", [
    "Nossa Equipe", "Fale Conosco", "Quem Somos", "Trabalhe Conosco", "Corpo Clínico",
    "Política de Privacidade", "Agende Sua Consulta", "Manutenção Preventiva",
    "Soluções Completas", "Sobre Nós",
])
def test_brazilian_headings_are_not_a_person(text):
    assert people.split_person_name(text) is None


# ── titles ──

@pytest.mark.parametrize("title", [
    "Proprietário", "Proprietária", "Sócio-proprietário", "Sócia-proprietária", "Sócio proprietário",
    "Sócia-fundadora", "Sócio-diretor", "Sócia-administradora", "Fundador", "Fundadora",
    "Cofundadora", "Co-fundador e CEO", "Diretor-presidente", "Diretora presidente",
    "Diretor geral", "Diretora executiva", "CEO e fundadora", "Sócio", "Sócia", "Dono",
    "Dona da Padaria Exemplo", "Presidente",
])
def test_brazilian_owner_titles(title):
    assert people.is_owner_title(title)


@pytest.mark.parametrize("title", [
    "Vice-presidente", "Assistente da diretoria", "Auxiliar administrativo", "Diretor adjunto",
    "Subgerente", "Secretária da presidência", "Estagiária", "Ex-proprietário",
    "Antigo sócio", "Fundador (1952-2001)", "Fundadora falecida", "Gerente comercial",
    "Dentista", "Recepcionista",
])
def test_brazilian_titles_that_are_not_the_owner(title):
    assert not people.is_owner_title(title)


@pytest.mark.parametrize("title, targets, expected", [
    ("Proprietária", ["Owner"], True),
    ("Sócia-proprietária", ["Proprietário"], True),
    ("Diretora comercial", ["Diretor comercial"], True),
    ("Fundadora", ["Founder"], True),
    ("Diretor adjunto", ["Diretor"], False),
    ("Gerente de marketing", ["Proprietário"], False),
])
def test_title_matches_portuguese_and_feminine(title, targets, expected):
    assert people.title_matches(title, targets) is expected


def test_a_portuguese_only_icp_wants_the_owner():
    assert people.title_matches("", ["Proprietário", "Sócio"], owner=True)
    assert people.title_matches("", ["Sócia-proprietária"], owner=True)
    assert not people.title_matches("", ["Gerente de vendas"], owner=True)


def test_owner_rank_puts_the_owner_before_the_general_manager():
    assert people.owner_rank("Proprietária") > people.owner_rank("Fundador") \
        > people.owner_rank("Diretor geral") > people.owner_rank("Gerente de vendas") \
        > people.owner_rank("Recepcionista")
    assert people.owner_rank("Sócio") >= 80


@pytest.mark.parametrize("text", [
    "Sócia-proprietária", "Diretora comercial", "Gerente da unidade", "Coordenadora de atendimento",
    "Dentista responsável técnica", "Advogado associado",
])
def test_portuguese_titles_read_as_roles(text):
    assert title_looks_like_role(text)


@pytest.mark.parametrize("title, expected", [
    ("Sócia-proprietária", "c_suite"), ("Fundador", "c_suite"), ("Dono", "c_suite"),
    ("Diretor geral", "c_suite"), ("Diretora executiva", "c_suite"),
    ("Diretor adjunto", "director"), ("Diretora comercial", "director"),
    ("Gerente da unidade", "manager"), ("Supervisora de atendimento", "manager"), ("Recepcionista", "individual"),
])
def test_infer_seniority_reads_portuguese(title, expected):
    assert Scout._infer_seniority(None, title) == expected


# ── owners in sentences ──

@pytest.mark.parametrize("html, expected", [
    ("<p>Fundada em 1998 por Ana Paula de Oliveira, a clínica atende toda a região.</p>",
     ("Ana Paula de Oliveira", "Founder")),
    ("<p>A clínica foi fundada pelo Dr. Otávio Rezende em 1987.</p>", ("Otávio Rezende", "Founder")),
    ("<p>Criada por Beatriz Monteiro, a Clínica Exemplo nasceu em Campinas.</p>",
     ("Beatriz Monteiro", "Founder")),
    ("<p>Hoje a clínica é comandada por Renato Albuquerque.</p>", ("Renato Albuquerque", "Owner")),
    ("<p>A empresa é administrada desde 2010 por Lúcia Prado.</p>", ("Lúcia Prado", "Owner")),
    ("<p>Conheça nossa sócia-proprietária, Beatriz Monteiro.</p>",
     ("Beatriz Monteiro", "Sócia-proprietária")),
    ("<p>Com a fundadora Cecília Brandão, a equipe cresceu.</p>", ("Cecília Brandão", "Fundadora")),
    ("<p>Renato Albuquerque, sócio-fundador da Clínica Exemplo, atende às segundas.</p>",
     ("Renato Albuquerque", "Sócio-fundador")),
    ("<p>Beatriz Monteiro é a proprietária da clínica desde 2010.</p>",
     ("Beatriz Monteiro", "Proprietária")),
    ("<p>Otávio Rezende fundou a clínica em 1987.</p>", ("Otávio Rezende", "Founder")),
    ("<p>Otávio Rezende fundou a Clínica Exemplo em 1987.</p>", ("Otávio Rezende", "Founder")),
])
def test_portuguese_owner_sentences(html, expected):
    assert prose(html) == [expected]


@pytest.mark.parametrize("html", [
    # someone else's business
    "<p>Beatriz Monteiro, fundadora da Quillnest, recomenda nossos serviços.</p>",
    "<p>Otávio Rezende fundou a Quillnest em 2001.</p>",
    # a title with no word making it this business's
    "<p>A presidente Lúcia Prado visitou a feira.</p>",
])
def test_portuguese_sentences_about_someone_else(html):
    assert prose(html) == []


def test_a_departed_founder_is_dropped():
    html = ("<p>Fundada em 1962 por Otávio Rezende, a clínica hoje é comandada por "
            "Beatriz Monteiro.</p><p>Otávio Rezende faleceu em 2009.</p>")
    found = people.find_people([("https://clinicaexemplo.test/sobre", html)], DOMAIN, SITE)
    assert [p["full_name"] for p in found] == ["Beatriz Monteiro"]


def test_first_person_bio_in_portuguese():
    html = ("<html><head><title>Beatriz Monteiro | Clínica Exemplo</title></head><body>"
            "<p>Eu fundei a clínica em 2004 depois de dez anos em hospital.</p></body></html>")
    assert prose(html) == [("Beatriz Monteiro", "Founder")]


# ── a whole team page ──

TEAM_PAGE = """
<nav><a href="/">Início</a> <a href="/fale-conosco">Fale Conosco</a></nav>
<h2>Nossa Equipe</h2>
<div class="card"><h3>Dra. Beatriz Monteiro</h3><p>Sócia-proprietária</p></div>
<div><strong>Renato Albuquerque</strong><br>Diretor comercial</div>
<span>João da Silva Santos<br>Sócio-fundador</span>
<div class="depoimentos">
  <p>"Atendimento impecável, recomendo!"</p>
  <p>Lúcia Prado, Fundadora da Quillnest</p>
</div>
"""


def test_portuguese_team_page_finds_the_owners_first():
    found = people.find_people([("https://clinicaexemplo.test/equipe", TEAM_PAGE)], DOMAIN, SITE)
    top = [(p["full_name"], p["owner"]) for p in found][:2]
    assert {name for name, _ in top} == {"Dra. Beatriz Monteiro", "João da Silva Santos"}
    assert all(owner for _, owner in top)
    assert "Lúcia Prado" not in [p["full_name"] for p in found]


def test_detect_people_flags_portuguese_decision_makers():
    by_last = {p["last_name"]: p for p in detect_people(TEAM_PAGE, domain=DOMAIN, site_name=SITE)}
    assert by_last["Monteiro"]["is_decision_maker"]
    assert by_last["Santos"]["is_decision_maker"]
    assert "Prado" not in by_last


def test_a_quoted_customer_with_a_portuguese_title_is_not_staff():
    assert people.title_names_other_org("Fundadora da Quillnest", DOMAIN, SITE)
    assert people.title_names_other_org("Diretor na Quillnest", DOMAIN, SITE)
    assert not people.title_names_other_org("Sócia-proprietária da Clínica Exemplo", DOMAIN, SITE)
    assert not people.title_names_other_org("Sócio Diretor", DOMAIN, SITE)
    assert not people.title_names_other_org("Diretora Presidente", DOMAIN, SITE)


def test_name_and_title_on_one_line():
    found = people.people_from_lines("<p>Beatriz Monteiro, Sócia-proprietária</p>", DOMAIN, SITE)
    assert [(p["full_name"], p["title"]) for p in found] == [("Beatriz Monteiro", "Sócia-proprietária")]


# ── which pages to read ──

def test_menu_links_follow_portuguese_team_and_about_pages():
    nav = ('<a href="/sobre-nos">Sobre nós</a><a href="/nossa-equipe">Nossa equipe</a>'
           '<a href="/fale-conosco">Fale conosco</a><a href="/blog/dicas">Blog</a>'
           '<a href="/trabalhe-conosco/vagas">Trabalhe conosco</a>'
           '<a href="/tratamentos/implantes">Implantes</a>')
    links = people.menu_links(nav, "https://clinicaexemplo.test/")
    assert links == ["https://clinicaexemplo.test/nossa-equipe",
                     "https://clinicaexemplo.test/sobre-nos",
                     "https://clinicaexemplo.test/fale-conosco"]


def test_menu_links_follow_quem_somos_and_corpo_clinico():
    nav = '<a href="/quem-somos">Quem somos</a><a href="/corpo-clinico">Corpo clínico</a>'
    assert set(people.menu_links(nav, "https://clinicaexemplo.test/")) == {
        "https://clinicaexemplo.test/quem-somos", "https://clinicaexemplo.test/corpo-clinico"}


# ── free discovery ──

@pytest.mark.parametrize("term, tags", [
    ("clínica odontológica", ["amenity=dentist"]),
    ("Dentista", ["amenity=dentist"]),
    ("escritório de advocacia", ["office=lawyer"]),
    ("Contabilidade", ["office=accountant"]),
    ("encanador", ["craft=plumber"]),
    ("Farmácia", ["amenity=pharmacy"]),
    ("padaria", ["shop=bakery"]),
    ("imobiliária", ["office=estate_agent"]),
    ("salão de beleza", ["shop=beauty", "shop=hairdresser"]),
    ("pet shop", ["shop=pet"]),
    ("clínica veterinária", ["amenity=veterinary"]),
    ("loja de materiais de construção", ["shop=doityourself", "shop=hardware"]),
])
def test_osm_maps_brazilian_business_terms(term, tags):
    assert OpenStreetMap()._tags(term) == tags
