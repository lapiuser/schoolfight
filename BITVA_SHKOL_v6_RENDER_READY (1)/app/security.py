from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict
from threading import Lock

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import (
    REDIS_URL,
    RATE_LIMIT_IP_TOTAL,
    RATE_LIMIT_SAME_POINT,
    RATE_LIMIT_SESSION_TOTAL,
    RATE_LIMIT_SESSION_SAME_POINT,
    SECRET_KEY,
    SESSION_MINUTES,
)

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="ksl-session")


def client_ip(request, trust_proxy: bool = False) -> str:
    if trust_proxy:
        # Only trust this header when the app is actually behind a trusted reverse proxy.
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()
    return request.client.host if request.client else "0.0.0.0"


def session_token(ip: str, user_agent: str) -> str:
    now = int(time.time())
    payload = {
        "ip": ip,
        "ua": hashlib.sha256(user_agent.encode("utf-8", "ignore")).hexdigest(),
        "iat": now,
        "nonce": hashlib.sha256(f"{SECRET_KEY}:{now}:{ip}:{user_agent}".encode()).hexdigest()[:20],
    }
    return _serializer.dumps(payload)


def validate_session(token: str | None, ip: str, user_agent: str) -> bool:
    if not token:
        return False
    try:
        data = _serializer.loads(token, max_age=SESSION_MINUTES * 60)
    except (BadSignature, SignatureExpired):
        return False
    expected_ua = hashlib.sha256(user_agent.encode("utf-8", "ignore")).hexdigest()
    return hmac.compare_digest(str(data.get("ip", "")), ip) and hmac.compare_digest(
        str(data.get("ua", "")), expected_ua
    )


def hash_ip(ip: str) -> str:
    return hashlib.sha256((SECRET_KEY + "|" + ip).encode()).hexdigest()


class RedisRateLimiter:
    def __init__(self, url: str):
        from redis.asyncio import Redis

        self.redis = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
        self.script = self.redis.register_script(
            """
            local same = redis.call('INCRBY', KEYS[1], ARGV[4])
            if same == tonumber(ARGV[4]) then redis.call('EXPIRE', KEYS[1], 2) end

            local ip_total = redis.call('INCRBY', KEYS[2], ARGV[4])
            if ip_total == tonumber(ARGV[4]) then redis.call('EXPIRE', KEYS[2], 2) end

            local session_total = redis.call('INCRBY', KEYS[3], ARGV[4])
            if session_total == tonumber(ARGV[4]) then redis.call('EXPIRE', KEYS[3], 2) end

            local session_same = redis.call('INCRBY', KEYS[4], ARGV[4])
            if session_same == tonumber(ARGV[4]) then redis.call('EXPIRE', KEYS[4], 2) end

            if same > tonumber(ARGV[1]) or ip_total > tonumber(ARGV[2]) or
               session_total > tonumber(ARGV[3]) or session_same > tonumber(ARGV[5]) then
              redis.call('DECRBY', KEYS[1], ARGV[4])
              redis.call('DECRBY', KEYS[2], ARGV[4])
              redis.call('DECRBY', KEYS[3], ARGV[4])
              redis.call('DECRBY', KEYS[4], ARGV[4])
              return 0
            end
            return 1
            """
        )

    async def consume(
        self,
        ip_key: str,
        session_key: str,
        institution_id: int,
        count: int,
        same_limit: int,
        total_limit: int,
        session_total_limit: int,
        session_same_limit: int,
    ) -> bool:
        now = int(time.time())
        keys = [
            f"bitva:rl:ip:{ip_key}:inst:{institution_id}:{now}",
            f"bitva:rl:ip:{ip_key}:all:{now}",
            f"bitva:rl:sess:{session_key}:all:{now}",
            f"bitva:rl:sess:{session_key}:inst:{institution_id}:{now}",
        ]
        result = await self.script(
            keys=keys,
            args=[same_limit, total_limit, session_total_limit, count, session_same_limit],
        )
        return bool(result)


class InMemoryRateLimiter:
    def __init__(self):
        self.lock = Lock()
        self.buckets: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))

    def _consume(self, key: str, now: int, count: int, limit: int) -> bool:
        sec, used = self.buckets[key]
        if sec != now:
            sec, used = now, 0
        if used + count > limit:
            return False
        self.buckets[key] = (sec, used + count)
        return True

    def consume(
        self,
        ip_key: str,
        session_key: str,
        institution_id: int,
        count: int,
        same_limit: int,
        total_limit: int,
        session_total_limit: int,
        session_same_limit: int,
    ) -> bool:
        now = int(time.time())
        keys = [
            (f"ip:{ip_key}:inst:{institution_id}", same_limit),
            (f"ip:{ip_key}:all", total_limit),
            (f"sess:{session_key}:all", session_total_limit),
            (f"sess:{session_key}:inst:{institution_id}", session_same_limit),
        ]
        with self.lock:
            snapshots = [(k, *self.buckets[k]) for k, _ in keys]
            if all(self._consume(k, now, count, limit) for k, limit in keys):
                return True
            for key, sec, used in snapshots:
                self.buckets[key] = (sec, used)
            return False


RATE_LIMITER = RedisRateLimiter(REDIS_URL) if REDIS_URL else InMemoryRateLimiter()
FALLBACK_LIMITER = InMemoryRateLimiter()


async def consume_rate_limit(ip_key: str, session_key: str, institution_id: int, count: int) -> bool:
    try:
        if isinstance(RATE_LIMITER, RedisRateLimiter):
            return await RATE_LIMITER.consume(
                ip_key,
                session_key,
                institution_id,
                count,
                RATE_LIMIT_SAME_POINT,
                RATE_LIMIT_IP_TOTAL,
                RATE_LIMIT_SESSION_TOTAL,
                RATE_LIMIT_SESSION_SAME_POINT,
            )
        return RATE_LIMITER.consume(
            ip_key,
            session_key,
            institution_id,
            count,
            RATE_LIMIT_SAME_POINT,
            RATE_LIMIT_IP_TOTAL,
            RATE_LIMIT_SESSION_TOTAL,
            RATE_LIMIT_SESSION_SAME_POINT,
        )
    except Exception:
        # Fail closed with the in-memory limiter if Redis is temporarily unavailable.
        return FALLBACK_LIMITER.consume(
            ip_key,
            session_key,
            institution_id,
            count,
            min(RATE_LIMIT_SAME_POINT, 30),
            min(RATE_LIMIT_IP_TOTAL, 100),
            min(RATE_LIMIT_SESSION_TOTAL, 100),
            min(RATE_LIMIT_SESSION_SAME_POINT, 30),
        )


class VisitRateLimiter:
    """Best-effort guard for new-visitor inflation. Uses Redis when available and
    an in-process fallback otherwise. It limits only *new visitor* registrations.
    """
    def __init__(self, redis_url: str):
        self._redis = None
        if redis_url:
            try:
                from redis.asyncio import Redis
                self._redis = Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
            except Exception:
                self._redis = None
        self._lock = Lock()
        self._local: dict[str, tuple[int, int]] = {}

    def _local_allow(self, key: str, now: int, limit: int, window: int) -> bool:
        bucket = now // max(1, window)
        old_bucket, used = self._local.get(key, (bucket, 0))
        if old_bucket != bucket:
            used = 0
            old_bucket = bucket
        if used >= limit:
            self._local[key] = (old_bucket, used)
            return False
        self._local[key] = (old_bucket, used + 1)
        return True

    async def allow(self, ip_key: str, limit: int, window: int) -> bool:
        now = int(time.time())
        if self._redis is not None:
            try:
                bucket = now // max(1, window)
                key = f"bitva:visit:{ip_key}:{bucket}"
                value = await self._redis.incr(key)
                if value == 1:
                    await self._redis.expire(key, max(2, window * 2))
                if value <= limit:
                    return True
                await self._redis.decr(key)
                return False
            except Exception:
                pass
        with self._lock:
            return self._local_allow(ip_key, now, limit, window)


VISIT_LIMITER = VisitRateLimiter(REDIS_URL)
