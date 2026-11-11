"""`sellbird setup` with manual product entry: the answers must reach sellbird.yaml
even though the repo already ships one, and the summary's "Product trained"
tick must reflect them. All data is fictional."""

import asyncio

import yaml

import sellbird.setup as setup

SHIPPED = {
    "persona": {"name": "Alex", "company": "Your Company", "email": "alex@yourcompany.com",
                "linkedin": "linkedin.com/in/alex-yourcompany"},
    "product": {"name": "Your Product", "key_benefits": ["Benefit 1"],
                "objection_responses": {"not interested": "No worries."}},
    "icp": {"industries": ["SaaS"], "titles": ["CMO"], "hiring_signals": []},
    "channels": {"email": {"enabled": True, "max_daily_sends": 50}},
    "usage": {"heartbeat_interval_minutes": 15},
}

# Step 5 answers, in the order _setup_product asks them.
ANSWERS = [
    "manual", "Acme Widgets", "WidgetPro", "Widgets for small bakeries", "$49/mo",
    "Saves an hour a day", "",                       # benefits, then finish
    "Bakeries, Cafes", "Owner, Head Baker", "1-20 employees", "Springfield, Illinois",
    "Monthly plan", "Free trial", "1",               # offer, entry, goal = book a call
    "https://calendar.example.com/acme", "20 minutes", "Sales lead",
    "Pat", "pat@acme.example", "Founder",
]


def test_manual_answers_survive_an_existing_sellbird_yaml(tmp_path, monkeypatch):
    config = tmp_path / "sellbird.yaml"
    config.write_text(yaml.safe_dump(SHIPPED, sort_keys=False))
    monkeypatch.setattr(setup, "CONFIG_FILE", config)
    monkeypatch.setattr(setup, "ENV_FILE", tmp_path / ".env")
    answers = list(ANSWERS)
    monkeypatch.setattr(setup, "_ask", lambda prompt, default="", **kw: answers.pop(0) or default)
    monkeypatch.setattr(setup, "_ask_yes_no", lambda prompt, default=True: True)

    wizard = setup.SetupWizard()
    asyncio.run(wizard._setup_product())
    assert answers == []
    wizard._write_config()

    saved = yaml.safe_load(config.read_text())
    assert saved["persona"]["company"] == "Acme Widgets"
    assert saved["persona"]["email"] == "pat@acme.example"
    assert saved["product"]["name"] == "WidgetPro"
    assert saved["product"]["key_benefits"] == ["Saves an hour a day"]
    assert saved["product"]["offer"]["booking_url"] == "https://calendar.example.com/acme"
    assert saved["icp"]["industries"] == ["Bakeries", "Cafes"]
    assert saved["icp"]["geography"] == ["Springfield", "Illinois"]
    # Blank answers don't wipe what was already in the file.
    assert saved["persona"]["linkedin"] == "linkedin.com/in/alex-yourcompany"
    assert saved["product"]["objection_responses"] == {"not interested": "No worries."}
    assert saved["icp"]["hiring_signals"] == []
    assert setup._product_configured()


def test_trained_config_is_not_overwritten_without_manual_entry(tmp_path, monkeypatch):
    trained = {**SHIPPED, "persona": {**SHIPPED["persona"], "company": "Example Co"}}
    config = tmp_path / "sellbird.yaml"
    config.write_text(yaml.safe_dump(trained, sort_keys=False))
    monkeypatch.setattr(setup, "CONFIG_FILE", config)

    wizard = setup.SetupWizard()
    wizard.config = {"persona": {"company": "Stale"}, "usage": {"heartbeat_interval_minutes": 5}}
    wizard._write_config()

    saved = yaml.safe_load(config.read_text())
    assert saved["persona"]["company"] == "Example Co"
    assert saved["usage"]["heartbeat_interval_minutes"] == 5
