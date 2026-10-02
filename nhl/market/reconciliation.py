"""Deterministic audit comparison of close-v1 and provider-reported closes."""

from __future__ import annotations

import csv
import io
import json
from hashlib import sha256
from typing import Any, Mapping

from nhl.market.settlement import SettlementRegistry
from nhl.timeutil import fmt_ts, parse_ts

SCHEMA_VERSION = "provider_close_reconciliation_v1"
COLUMNS = [
    "schema_version", "sport", "league", "provider", "provider_event_id", "internal_event_id",
    "event_match_status", "scheduled_start_utc", "sportsbook", "market_type", "selection",
    "internal_line", "internal_price", "internal_observed_at_utc", "provider_close_line",
    "provider_close_price", "provider_close_observed_at_utc", "line_delta", "price_delta",
    "exact_match", "comparable", "reconciliation_status", "settlement_attestation_status",
    "settlement_scope", "exclusion_reason", "internal_source_artifact", "provider_source_artifact",
    "reconciliation_run_id", "generated_at_utc",
]


def internal_close_legs(close_rows: list[dict]) -> list[dict]:
    out = []
    for row in close_rows:
        if row.get("status") != "AVAILABLE":
            continue
        prices = json.loads(row.get("prices") or "{}")
        legs = {str(x[0]): x for x in json.loads(row.get("legs") or "[]")}
        parts = str(row.get("market_id", "")).split(":")
        market = parts[1].split("@", 1)[0] if len(parts) > 1 else ""
        line = None
        if parts and parts[-1].startswith(("+", "-")):
            try: line = float(parts[-1])
            except ValueError: pass
        for selection, price in prices.items():
            leg = legs.get(selection, [selection, "", ""])
            leg_line = -line if market == "PUCK_LINE" and selection == "AWAY" and line is not None else line
            out.append({"internal_event_id": row.get("game_id", ""), "sportsbook": row.get("book", ""),
                        "market_type": market, "selection": selection, "line": leg_line, "price": price,
                        "observed_at": leg[1] if len(leg) > 1 else "", "artifact": leg[2] if len(leg) > 2 else ""})
    return out


def provider_close_legs(rows: list[dict], artifact: str = "") -> list[dict]:
    out = []
    for row in rows:
        base = {"provider_event_id": str(row.get("eventId", "")), "sportsbook": str(row.get("book", "")).lower(),
                "observed_at": "", "artifact": artifact}
        # lastUpdate is epoch seconds on the verified Owls endpoint.
        if isinstance(row.get("lastUpdate"), (int, float)):
            from datetime import datetime, timezone
            base["observed_at"] = fmt_ts(datetime.fromtimestamp(row["lastUpdate"], timezone.utc))
        ml = row.get("moneyline")
        if isinstance(ml, dict):
            for key, selection in (("home", "HOME"), ("away", "AWAY"), ("draw", "DRAW")):
                if ml.get(key) is not None:
                    out.append({**base, "market_type": "ML", "selection": selection, "line": None, "price": ml[key]})
        spread = row.get("spread")
        if isinstance(spread, dict):
            for side, selection in (("home", "HOME"), ("away", "AWAY")):
                if spread.get(side) is not None and spread.get(side + "Price") is not None:
                    out.append({**base, "market_type": "PUCK_LINE", "selection": selection,
                                "line": spread[side], "price": spread[side + "Price"]})
        total = row.get("total")
        if isinstance(total, dict) and total.get("line") is not None:
            for key, selection in (("overPrice", "OVER"), ("underPrice", "UNDER")):
                if total.get(key) is not None:
                    out.append({**base, "market_type": "TOTAL", "selection": selection,
                                "line": total["line"], "price": total[key]})
    return out


def reconcile_provider_closes(internal: list[dict], provider: list[dict], event_map: Mapping[str, str],
                              scheduled_starts: Mapping[str, object], registry: SettlementRegistry,
                              generated_at, provider_artifact: str = "") -> tuple[list[dict], dict]:
    generated = parse_ts(generated_at)
    run_seed = json.dumps([internal, provider, sorted(event_map.items()), fmt_ts(generated)], sort_keys=True, default=str)
    run_id = "PCR:" + sha256(run_seed.encode()).hexdigest()[:20]
    i_by = {(r["internal_event_id"], r["sportsbook"], r["market_type"], r["selection"]): r for r in internal}
    p_by, ambiguous_provider = {}, set()
    unmatched = []
    for p in provider:
        gid = event_map.get(p["provider_event_id"])
        if not gid:
            unmatched.append(p); continue
        key = (gid, p["sportsbook"], p["market_type"], p["selection"])
        if key in p_by and p_by[key] != p:
            ambiguous_provider.add(key)
        else:
            p_by[key] = p
    keys = sorted(set(i_by) | set(p_by))
    out = []
    def make(key, i, p, event_status="MATCHED"):
        gid, book, market, selection = key
        decision = registry.decision("owls", book, "NHL", market, generated)
        iline, pline = (i or {}).get("line"), (p or {}).get("line")
        iprice, pprice = (i or {}).get("price"), (p or {}).get("price")
        comparable = (i is not None and p is not None and decision.compatible and
                      bool(i.get("observed_at")) and bool(p.get("observed_at")))
        if event_status != "MATCHED": status = "EVENT_UNMATCHED"
        elif key in ambiguous_provider: status = "NON_COMPARABLE"
        elif not decision.compatible: status = "UNVERIFIED_SETTLEMENT"
        elif i is None:
            status = "BOOK_UNMATCHED" if any(k[0] == gid and k[2:] == (market, selection) for k in i_by) else (
                "MARKET_UNMATCHED" if any(k[0] == gid and k[1] == book for k in i_by) else "INTERNAL_MISSING")
        elif p is None:
            status = "BOOK_UNMATCHED" if any(k[0] == gid and k[2:] == (market, selection) for k in p_by) else (
                "MARKET_UNMATCHED" if any(k[0] == gid and k[1] == book for k in p_by) else "PROVIDER_CLOSE_MISSING")
        elif not comparable:
            status = "NON_COMPARABLE"
        else:
            if not isinstance(iprice, (int, float)) or not isinstance(pprice, (int, float)):
                status = "NON_COMPARABLE"
            else:
                ld, pd = iline != pline, iprice != pprice
                status = "LINE_AND_PRICE_DIFF" if ld and pd else "LINE_DIFF" if ld else "PRICE_DIFF" if pd else "EXACT"
        comparable = comparable and status not in {"NON_COMPARABLE"}
        line_delta = (float(pline) - float(iline)) if comparable and iline is not None and pline is not None else ""
        price_delta = (float(pprice) - float(iprice)) if comparable else ""
        return {"schema_version": SCHEMA_VERSION, "sport": "icehockey", "league": "NHL", "provider": "owls",
                "provider_event_id": (p or {}).get("provider_event_id", ""), "internal_event_id": gid,
                "event_match_status": event_status, "scheduled_start_utc": fmt_ts(scheduled_starts[gid]) if gid in scheduled_starts else "",
                "sportsbook": book, "market_type": market, "selection": selection,
                "internal_line": "" if iline is None else iline, "internal_price": "" if iprice is None else iprice,
                "internal_observed_at_utc": (i or {}).get("observed_at", ""),
                "provider_close_line": "" if pline is None else pline,
                "provider_close_price": "" if pprice is None else pprice,
                "provider_close_observed_at_utc": (p or {}).get("observed_at", ""),
                "line_delta": line_delta, "price_delta": price_delta, "exact_match": status == "EXACT",
                "comparable": comparable, "reconciliation_status": status,
                "settlement_attestation_status": decision.status, "settlement_scope": decision.scope,
                "exclusion_reason": "" if comparable else (decision.reason or status),
                "internal_source_artifact": (i or {}).get("artifact", ""),
                "provider_source_artifact": (p or {}).get("artifact", provider_artifact),
                "reconciliation_run_id": run_id, "generated_at_utc": fmt_ts(generated)}
    for key in keys: out.append(make(key, i_by.get(key), p_by.get(key)))
    for p in sorted(unmatched, key=lambda r: (r["provider_event_id"], r["sportsbook"], r["market_type"], r["selection"])):
        key = ("", p["sportsbook"], p["market_type"], p["selection"])
        out.append(make(key, None, p, "UNMATCHED"))
    out.sort(key=lambda r: (r["internal_event_id"], r["provider_event_id"], r["sportsbook"],
                            r["market_type"], r["selection"]))
    counts = {name: sum(r["reconciliation_status"] == name for r in out) for name in
              ("EXACT", "PRICE_DIFF", "LINE_DIFF", "LINE_AND_PRICE_DIFF", "INTERNAL_MISSING",
               "PROVIDER_CLOSE_MISSING", "EVENT_UNMATCHED", "BOOK_UNMATCHED", "MARKET_UNMATCHED",
               "UNVERIFIED_SETTLEMENT", "NON_COMPARABLE")}
    summary = {"schema_version": SCHEMA_VERSION, "reconciliation_run_id": run_id,
               "total_events_evaluated": len({r["internal_event_id"] or r["provider_event_id"] for r in out}),
               "comparable_markets": sum(bool(r["comparable"]) for r in out), **counts}
    return out, summary


def reconciliation_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO(); writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader(); writer.writerows(rows); return buf.getvalue().encode()
