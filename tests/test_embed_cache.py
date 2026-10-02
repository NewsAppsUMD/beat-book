"""The disk cache in front of embedding clients."""

import numpy as np

from embed_cache import CachingEmbedClient


class Counting:
    model_name = "count-test"
    dimensions = 4
    batch_size = 8
    max_parallel = 2

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(len(t)), float(t.count("a")), 0.1, 1 / 3] for t in texts]


def test_second_request_is_served_from_disk(tmp_path):
    inner = Counting()
    c = CachingEmbedClient(inner, tmp_path)
    first = c.embed(["alpha", "beta", "alpha"])
    assert inner.calls == [["alpha", "beta"]]            # each distinct text once
    again = CachingEmbedClient(Counting(), tmp_path)     # a new process, same cache file
    assert again.embed(["beta", "alpha"]) == [first[1], first[0]]
    assert again._inner.calls == [] and again.hits == 2
    # float32 throughout, as every caller converts to anyway
    assert first[0][3] == float(np.float32(1 / 3))


def test_only_misses_reach_the_model_and_models_do_not_mix(tmp_path):
    inner = Counting()
    c = CachingEmbedClient(inner, tmp_path)
    c.embed(["one", "two"])
    c.embed(["two", "three"])
    assert inner.calls[-1] == ["three"] and (c.hits, c.misses) == (1, 3)
    other = Counting(); other.model_name = "other-model"
    o = CachingEmbedClient(other, tmp_path)
    o.embed(["one"])
    assert other.calls == [["one"]]                      # a different model is a miss


def test_get_embed_client_can_turn_the_cache_off(monkeypatch):
    import embed_client
    monkeypatch.setattr(embed_client, "_make_embed_client", lambda m=None: Counting())
    monkeypatch.setenv("EMBED_CACHE", "off")
    assert isinstance(embed_client.get_embed_client(), Counting)
