from __future__ import annotations

from functools import lru_cache

from redis import Redis

from app.config import settings

THREAD_STEM_LOCK_KEY_PREFIX = "financial_analytics:thread_stem_lock:"


_RESERVE_STEMS_SCRIPT = """
for i=1,#KEYS do
    if redis.call('EXISTS', KEYS[i]) == 1 then
        return {0, KEYS[i]}
    end
end

for i=1,#KEYS do
    redis.call('SET', KEYS[i], ARGV[1], 'EX', ARGV[2])
end

return {1}
"""


_RELEASE_STEMS_SCRIPT = """
for i=1,#KEYS do
    if redis.call('GET', KEYS[i]) == ARGV[1] then
        redis.call('DEL', KEYS[i])
    end
end
return 1
"""


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _thread_stem_lock_key(thread_id: str, kind: str, stem: str) -> str:
    return f"{THREAD_STEM_LOCK_KEY_PREFIX}{thread_id}:{kind}:{stem}"


def reserve_thread_stems(thread_id: str, kind: str, stems: list[str], owner: str) -> str | None:
    if not stems:
        return None

    keys = [_thread_stem_lock_key(thread_id=thread_id, kind=kind, stem=stem) for stem in stems]
    res = get_redis_client().eval(
        _RESERVE_STEMS_SCRIPT,
        len(keys),
        *keys,
        owner,
        str(settings.STEM_LOCK_TTL_SECONDS),
    )

    if isinstance(res, list) and res and int(res[0]) == 1:
        return None

    if isinstance(res, list) and len(res) > 1:
        conflict_key = str(res[1])
        return conflict_key.rsplit(":", 1)[-1]

    return "unknown"


def release_thread_stems(thread_id: str, kind: str, stems: list[str], owner: str) -> None:
    if not stems:
        return

    keys = [_thread_stem_lock_key(thread_id=thread_id, kind=kind, stem=stem) for stem in stems]
    get_redis_client().eval(
        _RELEASE_STEMS_SCRIPT,
        len(keys),
        *keys,
        owner,
    )
