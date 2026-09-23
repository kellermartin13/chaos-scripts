#!/usr/bin/env python3

"""
Shared local Redis cache for Sleeper's NFL player map.

The player map (GET /players/nfl) is a large (~16 MB, ~12k record) payload that
changes at most once a day, and Sleeper asks callers not to fetch it more than
once per day. Both chaos.py and trade_review.py need it, so they share this one
cache — the same Redis key, TTL, and behavior.

Caching is a best-effort optimization: if the redis library isn't installed or
no server is reachable, callers transparently fall back to a live fetch.

For environments without Redis (e.g. GitHub Actions), a file cache can be
enabled by setting SLEEPER_PLAYERS_FILE to a JSON path; combined with
actions/cache keyed by date, it avoids re-fetching the ~16 MB player map on
every run. The file layer is used only when that path is provided.
"""

import json
import os
import time

try:
    import redis as _redis
except ImportError:  # redis is optional; caching degrades gracefully.
    _redis = None


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
PLAYERS_CACHE_KEY = "sleeper:players:nfl"
PLAYERS_CACHE_TTL_SECONDS = 24 * 60 * 60

# Optional file cache: when this env var points to a JSON path, the player map
# is read from / written to that file (used by CI via actions/cache). Unset =>
# file layer is inactive and behavior is unchanged.
PLAYERS_FILE_ENV = "SLEEPER_PLAYERS_FILE"


def read_players_file(path, ttl=PLAYERS_CACHE_TTL_SECONDS):
    """
    Load the player map from a JSON file if it exists and is fresher than ttl
    seconds. Returns the map dict, or None on a miss (missing, stale, or
    unreadable) so the caller falls back to the next layer.
    """

    try:
        if time.time() - os.path.getmtime(path) > ttl:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_players_file(path, data):
    """Persist the player map to a JSON file. Best-effort; errors are ignored."""

    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass


def _cache_to_redis(client, data):
    """Best-effort write-through to Redis; a None client is a no-op."""

    if client is None:
        return

    try:
        client.setex(
            PLAYERS_CACHE_KEY,
            PLAYERS_CACHE_TTL_SECONDS,
            json.dumps(data),
        )
    except Exception:
        pass


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


def cached_players(fetch, use_cache=True, client=None, players_file=None):
    """
    Return Sleeper's player map, served from cache when possible.

    Lookup order: Redis -> file cache -> live fetch. On a miss the fetched map
    is written back to whichever caches are active (Redis with a 24h TTL, and
    the file when SLEEPER_PLAYERS_FILE / players_file is set).

    fetch()      -- zero-arg callable that performs the live API fetch.
    use_cache    -- set False to bypass all caches entirely.
    client       -- inject a redis client (mainly for testing); when omitted
                    and use_cache is True, a local client is resolved.
    players_file -- JSON cache path (mainly for testing); when omitted it comes
                    from the SLEEPER_PLAYERS_FILE env var.

    Any cache error falls back to the next layer / a live fetch so caching
    never breaks the caller.
    """

    if use_cache and client is None:
        client = redis_client()

    if use_cache and players_file is None:
        players_file = os.environ.get(PLAYERS_FILE_ENV)

    # 1. Redis (shared across processes on a machine).
    if client is not None:
        try:
            cached = client.get(PLAYERS_CACHE_KEY)
            if cached:
                return json.loads(cached)
        except Exception:
            client = None  # fall through to the next layer

    # 2. File cache (persists across CI runs via actions/cache).
    if players_file:
        from_file = read_players_file(players_file)
        if from_file is not None:
            _cache_to_redis(client, from_file)  # warm Redis if available
            return from_file

    # 3. Live fetch, then populate whatever caches are active.
    data = fetch()

    _cache_to_redis(client, data)
    if players_file:
        write_players_file(players_file, data)

    return data
