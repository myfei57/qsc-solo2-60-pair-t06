"""Hash chained, append-only audit for every level-loop decision.

The decision dispatched to the field is the same object that is appended
here and served to the console. Each entry stores the SHA-256 of a canonical
rendering of the previous hash plus its own payload, so the trail is tamper
evident: dropping or editing an entry breaks every later link.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

LEVEL_AUDIT_KEY = "clearwell:level:audit"
GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class LevelEntry:
    seq: int
    at: str
    prev_hash: str
    hash: str
    payload: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "at": self.at,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "decision": self.payload,
        }


def _digest(prev_hash: str, payload: dict[str, object]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


class LevelAuditor:
    def __init__(self, store: Store) -> None:
        self._store = store

    def _rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for item in load_commands(self._store, LEVEL_AUDIT_KEY):
            try:
                decoded = json.loads(item)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                rows.append(decoded)
        return rows

    def append(self, payload: dict[str, object]) -> LevelEntry:
        rows = self._rows()
        seq = len(rows) + 1
        prev_hash = str(rows[-1]["hash"]) if rows else GENESIS_HASH
        stamped = dict(payload)
        stamped["seq"] = seq
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        stamped["at"] = at
        digest = _digest(prev_hash, stamped)
        entry = {"seq": seq, "at": at, "prev_hash": prev_hash, "hash": digest, "decision": stamped}
        append_command(self._store, LEVEL_AUDIT_KEY, json.dumps(entry, ensure_ascii=False))
        return LevelEntry(seq=seq, at=at, prev_hash=prev_hash, hash=digest, payload=stamped)

    def entries(self) -> list[LevelEntry]:
        result: list[LevelEntry] = []
        for row in self._rows():
            decision = row.get("decision")
            result.append(
                LevelEntry(
                    seq=int(row.get("seq", 0)),
                    at=str(row.get("at", "")),
                    prev_hash=str(row.get("prev_hash", "")),
                    hash=str(row.get("hash", "")),
                    payload=decision if isinstance(decision, dict) else {},
                )
            )
        return result

    def last(self) -> LevelEntry | None:
        entries = self.entries()
        return entries[-1] if entries else None

    def verify(self) -> tuple[bool, int]:
        """Walk the chain. Returns (ok, first_broken_seq)."""

        prev = GENESIS_HASH
        for row in self._rows():
            decision = row.get("decision")
            payload = decision if isinstance(decision, dict) else {}
            expected = _digest(prev, payload)
            if expected != row.get("hash") or row.get("prev_hash") != prev:
                return False, int(row.get("seq", -1))
            prev = str(row.get("hash"))
        return True, 0
