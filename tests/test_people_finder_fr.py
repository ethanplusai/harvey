"""Who decides at a French-language (Québec) small business.

Same shapes as test_people_finder.py, in French: an owner on a card, a
président named in a sentence, a founder in "fondée par", a customer quoted
in a « témoignage ». Every business and person here is fictional.
"""

import pytest

from sellbird.agents.scout import Scout
from sellbird.collectors import people
from sellbird.collectors.profile import ProfileCollector, title_looks_like_role

detect_people = ProfileCollector(state=None).detect_people

DOMAIN = "climatisationexemple.test"
SITE = "Climatisation Exemple"


def prose(html):
    return [(p["full_name"], p["title"]) for p in people.people_from_prose(html, DOMAIN, SITE)]


# ── names with accents ──

@pytest.mark.parametrize("text, expected", [
    ("Hélène Côté", ("Hélène", "Côté")),
    ("Jean-François Bélanger", ("Jean-François", "Bélanger")),
    ("Émile Gagné", ("Émile", "Gagné")),
    ("Julie Dubé-Lavoie", ("Julie", "Dubé-Lavoie")),
    ("Dre Hélène Côté", ("Hélène", "Côté")),
    ("M. Luc Fortier", ("Luc", "Fortier")),
    ("Mme Annie Pelletier", ("Annie", "Pelletier")),
])
def test_accented_names_are_names(text, expected):
    assert people.split_person_name(text) == expected


@pytest.mark.parametrize("text", [
    "Notre Équipe", "Nous Joindre", "Service Résidentiel", "Chauffage Climatisation",
    "Soumission Gratuite", "Qui Sommes", "Entretien Préventif",
])
def test_french_headings_are_not_a_person(text):
    assert not people.is_person_name(text)


# ── titles ──

@pytest.mark.parametrize("title", [
    "Propriétaire", "Copropriétaire", "Co-propriétaire", "Président", "Présidente",
    "PDG", "P.-D.G.", "Président-directeur général", "Présidente-directrice générale",
    "Fondateur", "Fondatrice", "Cofondateur", "Co-fondatrice",
    "Directeur général", "Directrice générale", "Président et fondateur",
    "Associé", "Associée principale",
    # accents dropped, as some sites type them
    "Proprietaire", "Presidente", "Directrice generale",
])
def test_french_owner_titles(title):
    assert people.is_owner_title(title)


@pytest.mark.parametrize("title", [
    "Vice-président", "Vice-présidente aux ventes", "Directeur général adjoint",
    "Adjointe à la présidence", "Assistante de direction", "Ancien président",
    "Fondateur retraité", "Associé aux ventes", "Directeur des ventes", "Technicien",
])
def test_french_titles_that_are_not_the_owner(title):
    assert not people.is_owner_title(title)


@pytest.mark.parametrize("title, targets, expected", [
    ("Présidente", ["Président"], True),
    ("Présidente", ["Owner", "Président"], True),
    ("Propriétaire", ["Owner"], True),
    ("Owner", ["Propriétaire"], True),
    ("Fondatrice", ["Fondateur"], True),
    ("Directrice générale", ["Directeur général"], True),
    ("Directrice des ventes", ["Directeur des ventes"], True),
    ("Directrice des ventes", ["directeur des ventes"], True),
    ("Directeur général adjoint", ["Directeur général"], False),
    ("Vice-présidente", ["Président"], False),
    ("Technicienne", ["Propriétaire", "Président"], False),
])
def test_title_matches_french_and_feminine(title, targets, expected):
    assert people.title_matches(title, targets) is expected


def test_a_french_only_icp_wants_the_owner():
    assert people.title_matches("", ["Propriétaire", "Président"], owner=True)
    assert people.title_matches("", ["Présidente", "Directrice générale"], owner=True)
    assert not people.title_matches("", ["Directeur des ventes"], owner=True)


def test_owner_rank_puts_the_owner_before_the_general_manager():
    assert people.owner_rank("Propriétaire") > people.owner_rank("Président") \
        > people.owner_rank("Directeur général") > people.owner_rank("Directeur des ventes") \
        > people.owner_rank("Technicien")
    assert people.owner_rank("Associé aux ventes") < people.owner_rank("Associé")


@pytest.mark.parametrize("text", [
    "Présidente-directrice générale", "Propriétaire", "Directrice des ventes et du service",
    "Technicienne en réfrigération", "Estimateur",
])
def test_french_titles_read_as_roles(text):
    assert title_looks_like_role(text)


@pytest.mark.parametrize("title, expected", [
    ("Propriétaire", "c_suite"),
    ("Présidente", "c_suite"),
    ("PDG", "c_suite"),
    ("Fondatrice", "c_suite"),
    ("Directrice générale", "c_suite"),
    ("Directeur général adjoint", "director"),
    ("Vice-présidente aux opérations", "vp"),
    ("Directeur des ventes", "director"),
    ("Gérante de succursale", "manager"),
    ("Technicien", "individual"),
    # English unchanged
    ("Owner", "c_suite"),
    ("Vice President of Sales", "vp"),
    ("Operations Manager", "manager"),
])
def test_infer_seniority_reads_french(title, expected):
    assert Scout._infer_seniority(None, title) == expected


# ── owners named in sentences ──

@pytest.mark.parametrize("html, expected", [
    ("<p>Fondée en 1987 par M. Gérald Lefebvre, l'entreprise dessert l'Estrie.</p>",
     ("Gérald Lefebvre", "Founder")),
    ("<p>Climatisation Exemple a été créée par Annie Pelletier.</p>",
     ("Annie Pelletier", "Founder")),
    ("<p>L'entreprise est dirigée par sa présidente Hélène Côté depuis 2010.</p>",
     ("Hélène Côté", "Présidente")),
    ("<p>Notre président et fondateur, Jean-François Bélanger, cumule 30 ans d'expérience.</p>",
     ("Jean-François Bélanger", "Président et fondateur")),
    ("<p>Marie-Ève Gagné, propriétaire de Climatisation Exemple, vous accueille.</p>",
     ("Marie-Ève Gagné", "Propriétaire")),
    ("<p>Luc Fortier est le propriétaire de l'entreprise depuis 2005.</p>",
     ("Luc Fortier", "Propriétaire")),
    ("<p>Luc Fortier a fondé l'entreprise en 1992.</p>", ("Luc Fortier", "Founder")),
    ("<p>Luc Fortier fonde Climatisation Exemple en 1992.</p>", ("Luc Fortier", "Founder")),
    ("<p>L'entreprise est détenue par Simon Paquette et sa famille.</p>",
     ("Simon Paquette", "Owner")),
    ("<p>Avec le propriétaire actuel Luc Fortier, l'équipe a doublé.</p>",
     ("Luc Fortier", "Propriétaire")),
    ("<p>Hélène Côté, PDG</p>", ("Hélène Côté", "PDG")),
])
def test_french_owner_sentences(html, expected):
    assert prose(html) == [expected]


@pytest.mark.parametrize("html", [
    # someone else's business or association
    "<p>Marie-Ève Gagné, présidente de Quillnest, recommande nos services.</p>",
    "<p>Jean Fortier, président de l'Association des entrepreneurs, était présent.</p>",
    # a title with no word making it this business's
    "<p>La présidente Annie Pelletier a visité le salon.</p>",
])
def test_french_sentences_about_someone_else(html):
    assert prose(html) == []


def test_a_departed_founder_is_dropped():
    html = ("<p>Fondée en 1962 par Gérald Lefebvre, l'entreprise est aujourd'hui détenue par "
            "Simon Paquette.</p><p>Gérald Lefebvre est décédé en 2009.</p>")
    found = people.find_people([("https://climatisationexemple.test/a-propos", html)], DOMAIN, SITE)
    assert [p["full_name"] for p in found] == ["Simon Paquette"]


def test_a_historical_founder_gives_way_to_the_current_owner():
    html = ("<p>Fondée en 1975 par Gérald Lefebvre, Climatisation Exemple dessert l'Estrie.</p>"
            "<div><h3>Hélène Côté</h3><p>Propriétaire</p></div>")
    found = people.find_people([("https://climatisationexemple.test/equipe", html)], DOMAIN, SITE)
    assert [(p["full_name"], p["title"]) for p in found] == [("Hélène Côté", "Propriétaire")]


def test_first_person_bio_in_french():
    html = ("<html><head><title>Hélène Côté | Climatisation Exemple</title></head><body>"
            "<p>J'ai fondé l'entreprise en 1998 après dix ans comme frigoriste.</p></body></html>")
    assert prose(html) == [("Hélène Côté", "Founder")]


# ── a whole team page ──

TEAM_PAGE = """
<nav><a href="/">Accueil</a> <a href="/nous-joindre">Nous joindre</a></nav>
<h2>Notre équipe</h2>
<div class="card"><h3>Hélène Côté</h3><p>Présidente-directrice générale</p></div>
<div><strong>Jean-François Bélanger</strong><br>Directeur des ventes et du service</div>
<span>Marie-Ève Gagné<br>Copropriétaire</span>
<div class="temoignages">
  <p>« Service impeccable, je recommande! »</p>
  <p>Julie Roy, Fondatrice chez Quillnest</p>
</div>
"""


def test_french_team_page_finds_the_owners_first():
    found = people.find_people([("https://climatisationexemple.test/equipe", TEAM_PAGE)], DOMAIN, SITE)
    assert [(p["full_name"], p["title"], p["owner"]) for p in found] == [
        ("Marie-Ève Gagné", "Copropriétaire", True),
        ("Hélène Côté", "Présidente-directrice générale", True),
        ("Jean-François Bélanger", "Directeur des ventes et du service", False),
    ]


def test_detect_people_flags_french_decision_makers():
    by_name = {p["full_name"]: p for p in detect_people(TEAM_PAGE, domain=DOMAIN, site_name=SITE)}
    assert by_name["Hélène Côté"]["is_decision_maker"]
    assert by_name["Marie-Ève Gagné"]["is_decision_maker"]
    assert "Julie Roy" not in by_name


def test_a_quoted_customer_with_a_french_title_is_not_staff():
    assert people.title_names_other_org("Fondatrice chez Quillnest", DOMAIN, SITE)
    assert people.title_names_other_org("Présidente, Quillnest", DOMAIN, SITE)
    assert not people.title_names_other_org("Présidente, Climatisation Exemple", DOMAIN, SITE)
    assert not people.title_names_other_org("Président Directeur Général", DOMAIN, SITE)


def test_name_and_title_on_one_line_with_a_hyphenated_name():
    html = "<p>Marie-Ève Gagné, Propriétaire</p>"
    found = people.people_from_lines(html, DOMAIN, SITE)
    assert [(p["full_name"], p["title"]) for p in found] == [("Marie-Ève Gagné", "Propriétaire")]


# ── which pages to read ──

def test_menu_links_follow_french_team_and_about_pages():
    nav = ('<a href="/a-propos">À propos</a><a href="/notre-equipe">Notre équipe</a>'
           '<a href="/nous-joindre">Nous joindre</a><a href="/blogue/conseils">Blogue</a>'
           '<a href="/services/thermopompes">Thermopompes</a>')
    links = people.menu_links(nav, "https://climatisationexemple.test/")
    assert links == ["https://climatisationexemple.test/notre-equipe",
                     "https://climatisationexemple.test/a-propos",
                     "https://climatisationexemple.test/nous-joindre"]


def test_menu_links_read_an_accented_path():
    nav = '<a href="/%C3%A9quipe">Équipe</a>'
    assert people.menu_links(nav, "https://climatisationexemple.test/") == [
        "https://climatisationexemple.test/%C3%A9quipe"]
