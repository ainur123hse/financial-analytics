from __future__ import annotations

from functools import lru_cache

from redis import Redis

from app.config import settings

STEM_LOCK_KEY_PREFIX = "financial_analytics:stem_lock:"
ACTIVE_TASKS_KEY = "financial_analytics:active_tasks"
TASK_EXISTS_KEY_PREFIX = "financial_analytics:task_exists:"


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


def _stem_lock_key(stem: str) -> str:
    return f"{STEM_LOCK_KEY_PREFIX}{stem}"


def reserve_stems(stems: list[str], owner: str) -> str | None:
    if not stems:
        return None

    keys = [_stem_lock_key(stem) for stem in stems]
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
        return conflict_key.removeprefix(STEM_LOCK_KEY_PREFIX)

    return "unknown"


def release_stems(stems: list[str], owner: str) -> None:
    if not stems:
        return

    keys = [_stem_lock_key(stem) for stem in stems]
    get_redis_client().eval(
        _RELEASE_STEMS_SCRIPT,
        len(keys),
        *keys,
        owner,
    )


def mark_task_registered(task_id: str) -> None:
    key = f"{TASK_EXISTS_KEY_PREFIX}{task_id}"
    get_redis_client().set(name=key, value="1", ex=settings.TASK_RETENTION_SECONDS)


def is_task_registered(task_id: str) -> bool:
    key = f"{TASK_EXISTS_KEY_PREFIX}{task_id}"
    return bool(get_redis_client().exists(key))


def remove_task_registration(task_id: str) -> None:
    key = f"{TASK_EXISTS_KEY_PREFIX}{task_id}"
    get_redis_client().delete(key)


def add_active_task(task_id: str) -> None:
    get_redis_client().sadd(ACTIVE_TASKS_KEY, task_id)


def remove_active_task(task_id: str) -> None:
    get_redis_client().srem(ACTIVE_TASKS_KEY, task_id)


def active_task_count() -> int:
    return int(get_redis_client().scard(ACTIVE_TASKS_KEY))
