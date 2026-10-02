"""Topic labels must be short phrases, even when the labeling model writes
out its reasoning (GLM on Ollama did, and its deliberation became the label)."""

import numpy as np

import pipeline
from chat_provider import ChatResponse

GLM_REPLY = (
    "Let me analyze these articles. They're all about the Chicago Housing Authority (CHA): "
    "1. Lawsuit against CHA for failing to maintain vacant properties 2. HUD whistleblower "
    "... Good labels could be: - \"CHA Leadership Dispute\" - \"Housing Authority Leadership "
    "Fight\" - \"Housing Agency CEO Controversy\" The label should be 2-5 words. "
    "\"CHA Leadership Dispute\" — but CHA is a place-specific acronym. The instructions say"
)


def test_clean_label_keeps_short_labels_and_rejects_prose():
    assert pipeline.clean_label("City Budget Disputes") == "City Budget Disputes"
    assert pipeline.clean_label('  "Transit"  ') == "Transit"
    assert pipeline.clean_label("Label: Immigration Policy.") == "Immigration Policy"
    # Reasoning text: take the last short quoted phrase it settled on.
    assert pipeline.clean_label(GLM_REPLY) == "CHA Leadership Dispute"
    # Prose with nothing quoted is not a label.
    assert pipeline.clean_label("Let me think about what these stories have in common here.") is None
    assert pipeline.clean_label(None) is None


STORIES = [
    {"title": "CHA appoints Keith Pettigrew as new CEO", "content": "word " * 60},
    {"title": "Housing advocates sue CHA over CEO appointment", "content": "word " * 60},
    {"title": "Mayor looks to oust CHA board chair", "content": "word " * 60},
    {"title": "Preckwinkle faces rival in Democratic primary", "content": "word " * 60},
    {"title": "Board of Review incumbent faces primary challenge", "content": "word " * 60},
    {"title": "Primary results for Cook County Board of Review", "content": "word " * 60},
]
LABELS = np.array([0, 0, 0, 1, 1, 1])
REDUCED = np.random.default_rng(0).normal(size=(6, 5))


class FakeProvider:
    label_model = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, dict):
            return ChatResponse(content=[{"type": "tool_use", "id": "t", "name": kw["tool_choice"]["name"],
                                          "input": reply}], stop_reason="tool_use")
        return ChatResponse(content=[{"type": "text", "text": reply}], stop_reason="end_turn")


def test_labels_are_requested_as_a_forced_tool_call():
    p = FakeProvider([{"labels": [{"cluster": 0, "label": "CHA Leadership Dispute"},
                                  {"cluster": 1, "label": "Cook County Primary Races"}]}])
    out = pipeline._label_all(p, STORIES, LABELS, REDUCED, "broad")
    assert out == {0: "CHA Leadership Dispute", 1: "Cook County Primary Races"}
    assert p.calls[0]["tool_choice"] == {"type": "tool", "name": "name_topics"}


def test_reasoning_in_place_of_labels_never_becomes_a_label():
    # Batch call returns prose; per-cluster calls return prose too, one with
    # a quoted label and one without.
    p = FakeProvider([GLM_REPLY, GLM_REPLY, "I need to consider the primary elections carefully here."])
    out = pipeline._label_all(p, STORIES, LABELS, REDUCED, "broad")
    assert out[0] == "CHA Leadership Dispute"
    assert set(out[1].split()) == {"Primary", "Board", "Review"}    # from its headlines
    assert all(len(v.split()) <= pipeline.MAX_LABEL_WORDS for v in out.values())


def test_unusable_batch_label_is_redone_and_duplicates_stay_distinct():
    p = FakeProvider([{"labels": [{"cluster": 0, "label": "x " * 40}, {"cluster": 1, "label": "Local News"}]},
                      {"label": "Local News"}])
    out = pipeline._label_all(p, STORIES, LABELS, REDUCED, "broad")
    assert sorted(out.values()) == ["Local News", "Local News (2)"]
    assert p.calls[1]["tool_choice"] == {"type": "tool", "name": "name_topic"}


def test_a_failed_label_call_falls_back_to_headline_words():
    class Broken(FakeProvider):
        def create(self, **kw):
            raise RuntimeError("model down")
    out = pipeline._label_all(Broken([]), STORIES, LABELS, REDUCED, "broad")
    assert set(out[0].split()) >= {"CHA", "CEO"} and "Faces" not in out[1]
    assert set(out[1].split()) == {"Primary", "Board", "Review"}


def _ollama(monkeypatch, content):
    import httpx
    import chat_provider as cp
    sent = []

    def handler(request):
        import json
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content},
                                          "done": True})
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("OLLAMA_CHAT_HOST", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "glm-5.3:cloud")
    return cp.OllamaChatProvider(), sent


def test_ollama_labels_use_a_json_schema_and_come_back_clean(monkeypatch):
    provider, sent = _ollama(monkeypatch, '{"label": "CHA Leadership Dispute"}')
    idx = [0, 1, 2]
    assert pipeline._label_cluster(provider, STORIES, idx, REDUCED[idx]) == "CHA Leadership Dispute"
    assert sent[0]["format"]["properties"]["label"]["type"] == "string"


def test_ollama_model_that_ignores_the_schema_still_gives_a_label(monkeypatch):
    provider, _ = _ollama(monkeypatch, GLM_REPLY)
    idx = [0, 1, 2]
    assert pipeline._label_cluster(provider, STORIES, idx, REDUCED[idx]) == "CHA Leadership Dispute"


DEEPSEEK_REPLY = ('I\'ll call `label_claims` with a label for each id.\n\n```json\n{\n  "labels": [\n'
                  '    {"id": 0, "label": "fact"},\n    {"id": 1, "label": "analysis"}\n  ]\n}\n```')


def test_forced_tool_answered_in_prose_becomes_a_tool_call(monkeypatch):
    import claim_evidence as ce
    provider, _ = _ollama(monkeypatch, DEEPSEEK_REPLY)
    resp = provider.create(model="m", system="", messages=[{"role": "user", "content": "x"}],
                           tools=[ce._CLASSIFY_TOOL], tool_choice={"type": "tool", "name": "label_claims"})
    assert resp.stop_reason == "tool_use"
    block = resp.content[0]
    assert block["type"] == "tool_use" and block["name"] == "label_claims"
    assert block["input"]["labels"][1] == {"id": 1, "label": "analysis"}


def test_json_in_text_takes_the_last_object_with_the_required_keys():
    from chat_provider import _json_in_text
    text = ('For example {"labels": []} would be empty. Also {"note": 1}. '
            'Final: {"labels": [{"id": 0, "kind": "fact"}]} done.')
    assert _json_in_text(text, ["labels"]) == {"labels": [{"id": 0, "kind": "fact"}]}
    assert _json_in_text("no json here {not json}", ["labels"]) is None
    assert _json_in_text('{"other": 1}', ["labels"]) is None


def test_ollama_topic_label_in_prose_and_json(monkeypatch):
    provider, _ = _ollama(monkeypatch, 'Here is the label:\n```json\n{"label": "CHA Leadership Dispute"}\n```')
    idx = [0, 1, 2]
    assert pipeline._label_cluster(provider, STORIES, idx, REDUCED[idx]) == "CHA Leadership Dispute"
