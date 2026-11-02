"""Company fit: the 0-100 score on the dashboard's Companies tab."""

from types import SimpleNamespace

from harvey.fit import company_fit


def config(dnc=()):
    return SimpleNamespace(
        icp=SimpleNamespace(
            industries=["forging company", "packaging manufacturer"],
            titles=["Plant Head", "Managing Director"],
            hiring_signals=["Forklift Operator", "Store Keeper"],
        ),
        compliance=SimpleNamespace(do_not_contact=list(dnc)),
    )


ACME = {"id": "c1", "name": "Acme Forge", "domain": "acme.example", "industry": "forging company"}


def person(title="Managing Director", status="verified", first="Nora", state="new"):
    return {"first_name": first, "title": title, "email_status": status, "status": state}


def test_full_fit_scores_100_with_every_reason():
    score, reasons = company_fit(
        ACME, [person()],
        [{"signal_code": "HIRING_ROLE", "value_text": "Forklift Operator - Night shift"}],
        config())
    assert score == 100
    assert reasons == ["sector: forging company", "decision maker: Managing Director",
                       "verified email", "hiring: forklift operator"]


def test_do_not_contact_scores_zero():
    score, reasons = company_fit(ACME, [person()], [], config(dnc=["acme.example"]))
    assert (score, reasons) == (0, ["on the do-not-contact list"])


def test_off_icp_sector_and_title_score_less():
    other = {**ACME, "industry": "magazine"}
    score, reasons = company_fit(other, [person(title="Editor", status="risky")], [], config())
    assert score == 15 + 12
    assert "named contact (not a target title)" in reasons and "risky email" in reasons


def test_general_inbox_and_contact_form():
    inbox = person(title="General inbox", first="", status="")
    score, reasons = company_fit(
        ACME, [inbox], [{"signal_code": "CONTACT_FORM_URL", "value_text": "https://acme.example/contact"}],
        config())
    assert score == 20 + 5 + 5
    assert reasons[1:] == ["general inbox only", "contact form only"]


def test_a_lost_contact_counts_for_nothing():
    score, reasons = company_fit(ACME, [person(state="lost")], [], config())
    assert score == 20 and "no way to reach yet" in reasons


def test_careers_page_without_a_matching_role_is_a_weak_signal():
    obs = [{"signal_code": "HIRING_ROLE", "value_text": "Apply Now"},
           {"signal_code": "HIRING_ROLE", "value_text": "Forklifting enthusiast"}]
    score, reasons = company_fit(ACME, [], obs, config())
    assert "careers page, no matching role" in reasons
    assert score == 20 + 5


def test_decision_maker_named_on_site_but_not_a_contact():
    obs = [{"signal_code": "DECISION_MAKER_TITLE", "value_text": "Ravi Example — ~ Plant Head"}]
    score, reasons = company_fit(ACME, [], obs, config())
    assert "decision maker: Plant Head" in reasons
    assert score == 20 + 30



def test_a_closed_role_stops_counting():
    obs = [{"signal_code": "HIRING_ROLE", "value_text": "Store Keeper", "value_num": None,
            "observed_at": "2026-01-01 10:00"},
           {"signal_code": "HIRING_ROLE", "value_text": "", "value_num": 0,
            "observed_at": "2026-02-01 10:00"}]
    score, reasons = company_fit(ACME, [person()], obs, config())
    assert "careers page, no matching role" in reasons and score == 20 + 30 + 25 + 5


def test_an_opted_out_contact_doesnt_count():
    score, reasons = company_fit(ACME, [person(state="opted_out")], [], config())
    assert score == 20 and "no way to reach yet" in reasons


def test_a_contact_on_the_do_not_contact_list_doesnt_count():
    jo = {**person(), "email": "jo@acme.example"}
    score, _ = company_fit(ACME, [jo], [], config(dnc=["jo@acme.example"]))
    assert score == 20
