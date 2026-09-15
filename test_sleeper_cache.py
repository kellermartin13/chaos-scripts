"""
Tests for sleeper_cache.py — the shared local Redis cache for Sleeper's player
map that both chaos.py and trade_review.py use.

Redis is faked in-memory; no server or network is required.
"""

import json

import sleeper_cache
import chaos
import trade_review as tr


class FakeRedis:
    """Minimal in-memory stand-in for a redis client."""

    def __init__(self):
        self.store = {}
        self.setex_calls = 0

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.setex_calls += 1
        self.ttl = ttl
        self.store[key] = value


class TestCachedPlayers:

    def test_cache_miss_fetches_and_populates(self):
        fake = FakeRedis()
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return {"p1": {"full_name": "Cached Guy"}}

        result = sleeper_cache.cached_players(fetch, client=fake)

        assert result == {"p1": {"full_name": "Cached Guy"}}
        assert calls["n"] == 1
        assert fake.setex_calls == 1
        assert fake.ttl == sleeper_cache.PLAYERS_CACHE_TTL_SECONDS

    def test_cache_hit_skips_fetch(self):
        fake = FakeRedis()
        fake.store[sleeper_cache.PLAYERS_CACHE_KEY] = json.dumps(
            {"p1": {"a": 1}}
        )

        def fetch():
            raise AssertionError("should not fetch on a cache hit")

        result = sleeper_cache.cached_players(fetch, client=fake)

        assert result == {"p1": {"a": 1}}

    def test_no_redis_falls_back_to_live_fetch(self, monkeypatch):
        monkeypatch.setattr(
            sleeper_cache, "redis_client", lambda url=sleeper_cache.REDIS_URL: None
        )

        result = sleeper_cache.cached_players(
            lambda: {"p2": {"full_name": "Live Guy"}}
        )

        assert result == {"p2": {"full_name": "Live Guy"}}

    def test_use_cache_false_bypasses_redis(self):
        fake = FakeRedis()
        fake.store[sleeper_cache.PLAYERS_CACHE_KEY] = json.dumps({"stale": 1})

        result = sleeper_cache.cached_players(
            lambda: {"fresh": 1}, use_cache=False, client=None
        )

        # Bypassed the (stale) cache and did not populate it.
        assert result == {"fresh": 1}

    def test_redis_read_error_falls_back(self):
        class BrokenRedis(FakeRedis):
            def get(self, key):
                raise RuntimeError("redis down")

        result = sleeper_cache.cached_players(
            lambda: {"ok": 1}, client=BrokenRedis()
        )

        assert result == {"ok": 1}


class TestBothScriptsShareCache:
    """Both scripts must resolve the same cached payload via the shared key."""

    def test_trade_review_uses_shared_cache(self, monkeypatch):
        fake = FakeRedis()
        fake.store[sleeper_cache.PLAYERS_CACHE_KEY] = json.dumps({"x": 1})

        monkeypatch.setattr(
            tr, "get_json",
            lambda url: (_ for _ in ()).throw(AssertionError("no fetch")),
        )

        assert tr.get_players(client=fake) == {"x": 1}

    def test_chaos_uses_shared_cache(self, monkeypatch):
        fake = FakeRedis()
        fake.store[sleeper_cache.PLAYERS_CACHE_KEY] = json.dumps({"y": 2})

        # chaos.get_sleeper_players resolves the client via sleeper_cache; point
        # that at our fake and ensure no live fetch happens.
        monkeypatch.setattr(
            sleeper_cache, "redis_client",
            lambda url=sleeper_cache.REDIS_URL: fake,
        )
        monkeypatch.setattr(
            chaos, "get_json",
            lambda url: (_ for _ in ()).throw(AssertionError("no fetch")),
        )

        assert chaos.get_sleeper_players() == {"y": 2}
