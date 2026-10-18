# storage/auth/challenge_store.py
"""
Challenge store (in-memory).

- Replay prevention: single-use challenges (consumed flag).
- Hygiene: opportunistic purge of expired challenges.
- Production can swap to Redis behind same interface.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass
class Challenge:
    challenge_id: str
    device_id: str
    nonce: str              # base64(32 bytes)
    issued_at: datetime
    expires_at: datetime
    consumed: bool = False


MAX_ACTIVE_CHALLENGES = 10_000


class InMemoryChallengeStore:
    def __init__(self):
        self._store: dict[str, Challenge] = {}

    def purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired_ids = [cid for cid, c in self._store.items() if now > c.expires_at]
        for cid in expired_ids:
            self._store.pop(cid, None)

    def issue(self, device_id: str, ttl_seconds: int) -> Challenge:
        self.purge_expired()

        if len(self._store) >= MAX_ACTIVE_CHALLENGES:
            raise RuntimeError("Challenge store at capacity")

        challenge_id = str(uuid.uuid4())
        nonce = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        issued_at = datetime.now(timezone.utc)
        expires_at = issued_at + timedelta(seconds=ttl_seconds)

        c = Challenge(
            challenge_id=challenge_id,
            device_id=device_id,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
            consumed=False,
        )
        self._store[challenge_id] = c
        return c

    def get(self, challenge_id: str) -> Optional[Challenge]:
        # Opportunistic purge on reads too (not just issue)
        self.purge_expired()
        # Return challenge if it exists, even if expired
        # This allows is_valid() to detect expiry and return proper error code
        return self._store.get(challenge_id)

    def consume(self, challenge_id: str) -> bool:
        c = self._store.get(challenge_id)
        if not c:
            return False
        if c.consumed:
            return False
        c.consumed = True
        return True

    def is_valid(self, c: Challenge) -> tuple[bool, str | None]:
        now = datetime.now(timezone.utc)
        if c.consumed:
            return False, "AUTH.CHALLENGE_CONSUMED"
        if now > c.expires_at:
            return False, "AUTH.CHALLENGE_EXPIRED"
        return True, None


class PostgresChallengeStore:
    """
    PostgreSQL-backed challenge store. Survives API restarts.

    Uses a `challenges` table (auto-created if missing).
    Same interface as InMemoryChallengeStore.
    """

    def __init__(self, dsn: Optional[str] = None):
        self._dsn = dsn
        self._ensure_table()

    def _ensure_table(self):
        from storage.postgres.db import get_connection
        with get_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS challenges (
                        challenge_id VARCHAR(255) PRIMARY KEY,
                        device_id    VARCHAR(255) NOT NULL,
                        nonce        TEXT NOT NULL,
                        issued_at    TIMESTAMPTZ NOT NULL,
                        expires_at   TIMESTAMPTZ NOT NULL,
                        consumed     BOOLEAN NOT NULL DEFAULT FALSE
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_challenges_device
                    ON challenges(device_id)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_challenges_expires
                    ON challenges(expires_at)
                """)
                conn.commit()

    def purge_expired(self) -> None:
        from storage.postgres.db import get_connection
        with get_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM challenges WHERE expires_at < NOW()"
                )
                conn.commit()

    def issue(self, device_id: str, ttl_seconds: int) -> Challenge:
        self.purge_expired()

        from storage.postgres.db import get_connection
        with get_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                # Check capacity
                cur.execute("SELECT COUNT(*) FROM challenges WHERE NOT consumed")
                count = cur.fetchone()[0]
                if count >= MAX_ACTIVE_CHALLENGES:
                    raise RuntimeError("Challenge store at capacity")

                challenge_id = str(uuid.uuid4())
                nonce = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
                issued_at = datetime.now(timezone.utc)
                expires_at = issued_at + timedelta(seconds=ttl_seconds)

                cur.execute("""
                    INSERT INTO challenges (challenge_id, device_id, nonce,
                                            issued_at, expires_at, consumed)
                    VALUES (%s, %s, %s, %s, %s, FALSE)
                """, (challenge_id, device_id, nonce, issued_at, expires_at))
                conn.commit()

        return Challenge(
            challenge_id=challenge_id,
            device_id=device_id,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
            consumed=False,
        )

    def get(self, challenge_id: str) -> Optional[Challenge]:
        from storage.postgres.db import get_connection
        with get_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT challenge_id, device_id, nonce,
                           issued_at, expires_at, consumed
                    FROM challenges
                    WHERE challenge_id = %s
                """, (challenge_id,))
                r = cur.fetchone()
                if not r:
                    return None
                return Challenge(
                    challenge_id=r[0], device_id=r[1], nonce=r[2],
                    issued_at=r[3], expires_at=r[4], consumed=r[5],
                )

    def consume(self, challenge_id: str) -> bool:
        from storage.postgres.db import get_connection
        with get_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE challenges SET consumed = TRUE
                    WHERE challenge_id = %s AND NOT consumed
                """, (challenge_id,))
                ok = cur.rowcount > 0
                conn.commit()
                return ok

    def is_valid(self, c: Challenge) -> tuple[bool, str | None]:
        now = datetime.now(timezone.utc)
        if c.consumed:
            return False, "AUTH.CHALLENGE_CONSUMED"
        if now > c.expires_at:
            return False, "AUTH.CHALLENGE_EXPIRED"
        return True, None
