"""The damaged-draft check and the startup model log."""

import draft_check as dc

GOOD = ("# CHA Beat Book\n\n*A guide*\n\n## Beat Overview\n\n" + "The CHA board voted 6-4 in March. " * 60
        + "\n\n## Key Sources & Players\n\n- **Keith Pettigrew**, CEO.\n\n## Calendar & Recurring Events\n\n- Monthly board meetings.\n")


def test_a_normal_book_passes():
    r = dc.check_draft(GOOD, 2000)
    assert r["ok"] and r["problems"] == []


def test_a_book_that_says_let_me_once_still_passes():
    assert dc.check_draft(GOOD.replace("## Beat Overview\n\n", "## Beat Overview\n\nLet me be clear about one thing. "), 2000)["ok"]


def test_glm_style_draft_is_flagged_for_each_sign():
    body = ("## Beat Overview\n\nText about the board and its votes in March.\n\n"
            "## Key Sources & Players\n\n- Pettigrew\n\n")
    reasoning = ("Word count check on my draft. I'll aim to keep it tight.\n"
                 "Let me resume mid-sentence where the draft stopped.\n"
                 "Hmm, is the general election on Nov 3?\n")
    draft = ("### The CHA CEO fight\n" + reasoning + body * 2 + "The CHA board voted. " * 3000)
    r = dc.check_draft(draft, 2000)
    assert not r["ok"]
    text = " ".join(r["problems"])
    assert "no title" in text and "against a target" in text
    assert "Beat Overview" in text and "Key Sources & Players" in text      # repeated sections
    assert "reasoning" in text and r["details"]["reasoning_lines"]


def test_env_overrides_are_recorded_and_logged(tmp_path, monkeypatch, capsys):
    import env_settings
    env = tmp_path / ".env"
    env.write_text("OLLAMA_CHAT_MODEL=deepseek-v4.1-flash:cloud\nTEST_ONLY_API_KEY=abc\nSAME_VALUE=x\n")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "glm-5.3:cloud")
    monkeypatch.setenv("TEST_ONLY_API_KEY", "shell-secret")
    monkeypatch.setenv("SAME_VALUE", "x")
    monkeypatch.setattr(env_settings, "ENV_OVERRIDES", [])
    overrides = env_settings.load_env(env)
    assert overrides == ["OLLAMA_CHAT_MODEL", "TEST_ONLY_API_KEY"]
    import os
    assert os.environ["OLLAMA_CHAT_MODEL"] == "glm-5.3:cloud"            # the shell still wins
    import app
    monkeypatch.setattr(app, "ENV_OVERRIDES", overrides)
    app.log_model_settings()
    out = capsys.readouterr().out
    assert "OLLAMA_CHAT_MODEL is set in the shell ('glm-5.3:cloud')" in out
    assert "TEST_ONLY_API_KEY is set in the shell and overrides" in out and "shell-secret" not in out


def test_existing_books_are_checked_once_at_startup(tmp_path, monkeypatch):
    import json
    import app
    import store
    monkeypatch.setattr(store, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY_PATH", tmp_path / "library.json")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "library.json").write_text(json.dumps([
        {"id": "good", "stem": "good_book", "status": "ready", "title": "Good"},
        {"id": "bad", "stem": "bad_book", "status": "ready", "title": "Bad"},
        {"id": "done", "stem": "bad_book", "status": "ready", "title": "Checked", "warning": ""},
    ]))
    (tmp_path / "good_book.md").write_text(GOOD)
    (tmp_path / "bad_book.md").write_text("### Notes\nLet me plan.\nI'll count words.\nWord count check.\n" + "Word " * 9000)
    assert app._check_existing_books() == 1
    by_id = {r["id"]: r for r in store.list_books()}
    assert by_id["good"]["warning"] == "" and "no title" in by_id["bad"]["warning"]
    assert by_id["done"]["warning"] == ""          # already checked: left alone


def test_long_in_one_pass_is_a_note_not_damage():
    long_book = GOOD.replace("The CHA board voted 6-4 in March. " * 60, "The CHA board voted 6-4 in March. " * 400)
    r = dc.check_draft(long_book, 1000, continuations=0)
    assert r["ok"] and r["problems"] == []
    assert "times the 1,000-word target" in r["notes"][0] and "one pass" in r["notes"][0]
    # After continuations, the same length points to a rewrite.
    r = dc.check_draft(long_book, 1000, continuations=2)
    assert not r["ok"] and "asked to continue" in r["problems"][0]


def test_a_length_only_warning_is_cleared_at_startup(tmp_path, monkeypatch):
    import json
    import app
    import store
    monkeypatch.setattr(store, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY_PATH", tmp_path / "library.json")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    (tmp_path / "library.json").write_text(json.dumps([
        {"id": "long", "stem": "long_book", "status": "ready", "title": "Long", "target_words": 1000,
         "warning": "It is 2,775 words against a target of about 1,000, which usually means ..."}]))
    (tmp_path / "long_book.md").write_text(GOOD.replace("The CHA board voted 6-4 in March. " * 60,
                                                        "The CHA board voted 6-4 in March. " * 400))
    (tmp_path / "long_book.manifest.json").write_text(json.dumps({"agent": {"final_write": {"continuation_rounds": 0}}}))
    assert app._check_existing_books() == 0
    assert store.list_books()[0]["warning"] == ""
