"""Explicit sportsbook settlement attestations; absence always fails closed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from nhl.timeutil import parse_ts

STATUSES = {"VERIFIED", "UNVERIFIED", "CONFLICTING", "EXPIRED"}
SCOPES = {"REGULATION", "FULL_GAME_OT_SO", "FULL_GAME_OT_NO_SO", "OTHER"}


@dataclass(frozen=True)
class SettlementAttestation:
    provider: str
    sportsbook: str
    league: str
    market_type: str
    settlement_scope: str
    includes_overtime: bool
    includes_shootout: bool
    evidence_type: str
    evidence_reference: str
    attested_by: str
    attested_at: datetime
    status: str
    notes: str = ""
    expires_at: datetime | None = None

    @classmethod
    def from_dict(cls, row: dict) -> "SettlementAttestation":
        required = ["provider", "sportsbook", "league", "market_type", "settlement_scope",
                    "includes_overtime", "includes_shootout", "evidence_type", "evidence_reference",
                    "attested_by", "attested_at", "status"]
        missing = [key for key in required if key not in row]
        if missing:
            raise ValueError(f"attestation missing fields: {missing}")
        status, scope = str(row["status"]).upper(), str(row["settlement_scope"]).upper()
        if status not in STATUSES or scope not in SCOPES:
            raise ValueError("invalid attestation status or settlement scope")
        if not isinstance(row["includes_overtime"], bool) or not isinstance(row["includes_shootout"], bool):
            raise ValueError("settlement booleans must be bool")
        return cls(str(row["provider"]).lower(), str(row["sportsbook"]).lower(), str(row["league"]).upper(),
                   str(row["market_type"]).upper(), scope, row["includes_overtime"], row["includes_shootout"],
                   str(row["evidence_type"]), str(row["evidence_reference"]), str(row["attested_by"]),
                   parse_ts(row["attested_at"]), status, str(row.get("notes", "")),
                   parse_ts(row["expires_at"]) if row.get("expires_at") else None)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.provider, self.sportsbook, self.league, self.market_type


@dataclass(frozen=True)
class SettlementDecision:
    status: str
    compatible: bool
    scope: str
    reason: str


class SettlementRegistry:
    def __init__(self, attestations: list[SettlementAttestation] | None = None) -> None:
        self._rows: dict[tuple[str, str, str, str], list[SettlementAttestation]] = {}
        for row in attestations or []:
            self._rows.setdefault(row.key, []).append(row)

    @classmethod
    def load(cls, path: str | Path) -> "SettlementRegistry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("settlement registry must be a JSON list")
        return cls([SettlementAttestation.from_dict(row) for row in raw])

    def decision(self, provider: str, sportsbook: str, league: str, market_type: str,
                 as_of: datetime) -> SettlementDecision:
        key = (provider.lower(), sportsbook.lower(), league.upper(), market_type.upper())
        rows = self._rows.get(key, [])
        if not rows:
            return SettlementDecision("UNVERIFIED", False, "", "UNVERIFIED_SETTLEMENT")
        active = [r for r in rows if r.attested_at <= parse_ts(as_of)]
        if not active:
            return SettlementDecision("UNVERIFIED", False, "", "ATTESTATION_NOT_YET_KNOWN")
        if any(r.status == "CONFLICTING" for r in active):
            return SettlementDecision("CONFLICTING", False, "", "CONFLICTING_SETTLEMENT")
        verified = [r for r in active if r.status == "VERIFIED" and
                    (r.expires_at is None or r.expires_at >= parse_ts(as_of))]
        signatures = {(r.settlement_scope, r.includes_overtime, r.includes_shootout) for r in verified}
        if len(signatures) > 1:
            return SettlementDecision("CONFLICTING", False, "", "CONFLICTING_SETTLEMENT")
        if not verified:
            expired = any(r.status == "EXPIRED" or (r.expires_at and r.expires_at < parse_ts(as_of)) for r in active)
            return SettlementDecision("EXPIRED" if expired else "UNVERIFIED", False, "",
                                      "EXPIRED_SETTLEMENT" if expired else "UNVERIFIED_SETTLEMENT")
        row = sorted(verified, key=lambda r: (r.attested_at, r.evidence_reference))[-1]
        # Canonical NHL ML/PUCK_LINE/TOTAL contract includes both OT and SO.
        compatible = row.settlement_scope == "FULL_GAME_OT_SO" and row.includes_overtime and row.includes_shootout
        return SettlementDecision("VERIFIED", compatible, row.settlement_scope,
                                  "" if compatible else "INCOMPATIBLE_SETTLEMENT_SCOPE")
