#!/usr/bin/env python3

"""
Shared local Redis cache for Sleeper's NFL player map.

The player map (GET /players/nfl) is a large (~16 MB, ~12k record) payload that
changes at most once a day, and Sleeper asks callers not to fetch it more than
once per day. Both chaos.py and trade_review.py need it, so they share this one
cache — the same Redis key, TTL, and behavior.

Caching is a best-effort optimization: if the redis library isn't installed or
no server is reachable, callers transparently fall back to a live fetch.
"""

import json
import os

try:
    import redis as _redis
except ImportError:  # redis is optional; caching degrades gracefully.
    _redis = None


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
PLAYERS_CACHE_KEY = "sleeper:players:nfl"
PLAYERS_CACHE_TTL_SECONDS = 24 * 60 * 60


def redis_client(url=REDIS_URL):
    """
    Connect to the local Redis, or return None if the redis library isn't
    installed or the server can't be reached. A None client means "no cache";
    callers must fall back to a live fetch.
    """

    if _redis is None:
        return None

    try:
        client = _redis.Redis.from_url(url)
        client.ping()
        return client
    except Exception:
        return None


def cached_players(fetch, use_cache=True, client=None):
    """
    Return Sleeper's player map, served from Redis when possible.

    fetch()   -- zero-arg callable that performs the live API fetch.
    use_cache -- set False to bypass the cache entirely.
    client    -- inject a redis client (mainly for testing); when omitted and
                 use_cache is True, a local client is resolved automatically.

    On a cache miss the fetched map is written back with a 24h TTL. Any Redis
    error falls back to a live fetch so caching never breaks the caller.
    """

    if use_cache and client is None:
        client = redis_client()

    if client is not None:
        try:
            cached = client.get(PLAYERS_CACHE_KEY)
            if cached:
                return json.loads(cached)
        except Exception:
            client = None  # fall through to a live fetch

    data = fetch()

    if client is not None:
        try:
            client.setex(
                PLAYERS_CACHE_KEY,
                PLAYERS_CACHE_TTL_SECONDS,
                json.dumps(data),
            )
        except Exception:
            pass  # caching is best-effort

    return data
