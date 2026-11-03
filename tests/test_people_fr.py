"""French small-business sites: lawyers ("Maître Lise VERNIER"), doctors,
French titles ("Avocat associé", "Gérante") and French menus ("Notre équipe").

Fixtures are fictional; they rebuild the markup shapes French law-firm sites use."""

import pytest

from harvey.collectors import people


def names(found):
    return [(p["first_name"], p["last_name"]) for p in found]


@pytest.mark.parametrize("text,expected", [
    ("Maître Anaïs Morand", ("Anaïs", "Morand")),
    ("Maître Lise VERNIER", ("Lise", "Vernier")),
    ("Maitre Lise Vernier", ("Lise", "Vernier")),
    ("Me Jérôme Ferrand", ("Jérôme", "Ferrand")),
    ("Maître Pierre-Henri GALLAND", ("Pierre-Henri", "Galland")),
    ("Docteur Hélène ROUX", ("Hélène", "Roux")),
    ("Thibault RENAUD", ("Thibault", "Renaud")),
    ("Éric de LA FONTAINE", ("Éric", "de La Fontaine")),
    ("Mme Anne-Sophie Lefèvre", ("Anne-Sophie", "Lefèvre")),
])
def test_french_names_and_honorifics(text, expected):
    assert people.split_person_name(text) == expected


@pytest.mark.parametrize("text", [
    "Cabinet XYZ", "Notre Équipe", "Avocat Associé", "Mentions Légales",
    "Domaines Compétences", "Prendre Rendez-vous", "Cabinet SCP", "Maître",
    "Droit Social", "Nos Avocats",
])
def test_french_headings_are_not_people(text):
    assert people.split_person_name(text) is None


@pytest.mark.parametrize("title,owner", [
    ("Avocat associé", True),
    ("AVOCAT ASSOCIE", True),
    ("Avocate associée", True),
    ("Associé fondateur", True),
    ("Fondatrice", True),
    ("Gérante", True),
    ("Président", True),
    ("Notaire associé", True),
    ("Avocat collaborateur", False),
    ("Assistante juridique", False),
    ("Vice-président", False),
    ("Avocat honoraire", False),
    ("Ancien associé", False),
])
def test_french_owner_titles(title, owner):
    assert people.is_owner_title(title) is owner


def test_french_titles_satisfy_a_french_icp():
    icp = ["Avocat associé", "Avocat fondateur", "Gérant"]
    assert people.title_matches("AVOCAT ASSOCIE", icp)
    assert people.title_matches("Associée", icp)
    assert people.title_matches("Gérante", icp)
    assert not people.title_matches("Avocat collaborateur", icp)
    assert people.title_matches("", icp, owner=True)


def test_name_line_then_french_title_line():
    html = "<div><h3>Maître Lise VERNIER</h3><p>Gérante</p></div>"
    found = people.find_people([("https://www.avocat-vernier.test/", html)], "avocat-vernier.test")
    assert names(found)[0] == ("Lise", "Vernier")
    assert found[0]["owner"]


def test_team_card_with_shouted_title():
    html = ("<div class='membre'><h4>Thibault RENAUD</h4><span>AVOCAT ASSOCIE</span></div>"
            "<div class='membre'><h4>Chloé Marin</h4><span>AVOCAT COLLABORATEUR</span></div>")
    found = people.find_people([("https://renaud-associes.test/equipe/", html)], "renaud-associes.test")
    by_name = {(p["first_name"], p["last_name"]): p for p in found}
    assert by_name[("Thibault", "Renaud")]["owner"]
    assert not by_name[("Chloé", "Marin")]["owner"]
    assert names(found)[0] == ("Thibault", "Renaud")


def test_solo_lawyer_named_on_the_door_is_the_owner():
    html = ("<p>Bienvenue au cabinet.</p>"
            "<p>Maître Anaïs Morand vous reçoit sur rendez-vous.</p>")
    found = people.find_people([("https://morand-avocat.test/", html)], "morand-avocat.test")
    assert names(found) == [("Anaïs", "Morand")]
    assert found[0]["owner"]


def test_a_maitre_not_on_the_door_is_named_but_not_the_owner():
    html = "<p>Maître Inès Garnier intervient en droit social.</p>"
    found = people.find_people([("https://reseau-avocats.test/", html)], "reseau-avocats.test")
    assert names(found) == [("Inès", "Garnier")]
    assert not found[0]["owner"]


@pytest.mark.parametrize("sentence,who", [
    ("Le cabinet a été fondé en 1998 par Maître Jean DUPONT.", ("Jean", "Dupont")),
    ("Cabinet créé par Me Sophie Martin, avocate au barreau.", ("Sophie", "Martin")),
    ("Le cabinet est dirigé par Pierre Lambert depuis 2010.", ("Pierre", "Lambert")),
])
def test_french_founder_sentences(sentence, who):
    found = people.people_from_prose(f"<p>{sentence}</p>", "cabinet.example")
    assert names(found)[:1] == [who]
    assert found[0]["owner"]


def test_departed_founder_in_french_is_dropped():
    html = ("<p>Le cabinet a été fondé par Maître Paul Girard, décédé en 2015.</p>"
            "<div><h3>Claire Girard</h3><p>Avocate associée</p></div>")
    found = people.find_people([("https://girard-avocats.test/", html)], "girard-avocats.test")
    assert ("Paul", "Girard") not in names(found)
    assert names(found)[0] == ("Claire", "Girard")


def test_french_menu_links_are_followed():
    html = """<nav>
      <a href="/le-cabinet/">Le cabinet</a>
      <a href="/notre-equipe/">Notre équipe</a>
      <a href="/avocat-droit-du-travail/">Droit du travail</a>
      <a href="/nous-contacter/">Contact</a>
      <a href="/honoraires/">Honoraires</a>
    </nav>"""
    links = people.menu_links(html, "https://cabinet.example/")
    assert links[0] == "https://cabinet.example/notre-equipe/"
    assert "https://cabinet.example/le-cabinet/" in links
    assert "https://cabinet.example/nous-contacter/" in links
    assert not any("droit-du-travail" in u or "honoraires" in u for u in links)
