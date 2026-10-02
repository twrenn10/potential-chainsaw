import json
import urllib.parse
from datetime import datetime, timezone

from nhl.data.snapshots import RawSnapshotStore
from nhl.market.owls import HTTPResponse, OwlsInsightClient
from nhl.market.owls_history import capture_bulk_history, capture_offset_pages, normalize_archived_snapshots

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 20, tzinfo=UTC)


def client(tmp_path, responder, retries=0):
    return OwlsInsightClient(RawSnapshotStore(tmp_path), "secret", transport=responder,
                             max_retries=retries, sleep=lambda _: None, clock=lambda: NOW)


def page(records, more=False):
    return json.dumps({"success": True, "data": {"odds": records,
        "pagination": {"hasMore": more}}}).encode()


def test_one_page_and_valid_empty(tmp_path):
    c = client(tmp_path, lambda *_: HTTPResponse(200, page([], False), {}))
    result = capture_offset_pages(c, "/x", {}, ("data", "odds"), identity=lambda r: str(r["id"]))
    assert result.completeness == "EMPTY_VALID" and result.pages_completed == 1


def test_multiple_pages_dedupe_and_stable_sort(tmp_path):
    def respond(url, *_):
        offset = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["offset"][0])
        body = page([{"id": "b"}, {"id": "a"}], True) if offset == 0 else page([{"id": "b"}, {"id": "c"}], False)
        return HTTPResponse(200, body, {})
    result = capture_offset_pages(client(tmp_path, respond), "/x", {}, ("data", "odds"),
                                  identity=lambda r: r["id"], limit=2)
    assert [r["id"] for r in result.records] == ["a", "b", "c"] and result.duplicates == 1
    assert result.completeness == "COMPLETE" and result.pages_completed == 2


def test_repeated_page_and_page_limit_are_partial(tmp_path):
    repeated = client(tmp_path / "a", lambda *_: HTTPResponse(200, page([{"id": "a"}], True), {}))
    r = capture_offset_pages(repeated, "/x", {}, ("data", "odds"), identity=lambda x: x["id"], limit=1)
    assert r.completeness == "PARTIAL_PAGINATION_GUARD" and "REPEATED_PAGE" in r.errors
    changing = client(tmp_path / "b", lambda url, *_: HTTPResponse(200, page([{"id": url}], True), {}))
    r2 = capture_offset_pages(changing, "/x", {}, ("data", "odds"), identity=lambda x: x["id"],
                              limit=1, max_pages=2)
    assert r2.completeness == "PARTIAL_PAGINATION_GUARD" and "MAX_PAGES" in r2.errors


def test_auth_and_permanent_rate_limit_surface_partial(tmp_path):
    for status, expected in ((401, "AUTH_FAILURE"), (403, "AUTH_FAILURE"), (429, "PARTIAL_RATE_LIMIT")):
        c = client(tmp_path / str(status), lambda *_a, s=status: HTTPResponse(s, b"{}", {}), retries=0)
        r = capture_offset_pages(c, "/x", {}, ("data", "odds"), identity=lambda x: "x")
        assert r.completeness == expected


def test_429_then_success_and_raw_first(tmp_path):
    calls = iter([HTTPResponse(429, b'{"rate":true}', {"Retry-After": "0"}),
                  HTTPResponse(200, page([{"id": "a"}], False), {})])
    c = client(tmp_path, lambda *_: next(calls), retries=1)
    r = capture_offset_pages(c, "/x", {}, ("data", "odds"), identity=lambda x: x["id"])
    assert r.completeness == "COMPLETE" and len(list(c.store.entries("odds"))) == 2


def test_malformed_response_and_record_guard(tmp_path):
    c = client(tmp_path / "a", lambda *_: HTTPResponse(200, b'{"data":{}}', {}))
    assert capture_offset_pages(c, "/x", {}, ("data", "odds"), identity=lambda x: "x").completeness == "INVALID_RESPONSE"
    c2 = client(tmp_path / "b", lambda *_: HTTPResponse(200, page([{"id": "a"}, {"id": "b"}], False), {}))
    assert capture_offset_pages(c2, "/x", {}, ("data", "odds"), identity=lambda x: x["id"],
                                max_records=1).completeness == "PARTIAL_PAGINATION_GUARD"


def test_bulk_manifest_is_post_hoc_and_deterministic_with_fixed_inputs(tmp_path):
    def respond(url, *_):
        path = urllib.parse.urlsplit(url).path
        key = "games" if path.endswith("games") else "odds"
        data = {key: [], "pagination": {"hasMore": False}}
        return HTTPResponse(200, json.dumps({"success": True, "data": data}).encode(), {})
    manifest, entry = capture_bulk_history(client(tmp_path, respond), "NHL", "2026-09-01", "2026-09-02")
    assert manifest["completeness_status"] == "EMPTY_VALID"
    assert manifest["causality"] == "POST_HOC_ANALYSIS_ONLY" and not manifest["eligible_for_model_input"]
    assert entry.source == "owls_history_manifest"


def test_archive_normalization_requires_mapping_timestamp_and_pregame_phase():
    base = {"providerEventId": "e", "book": "pinnacle", "market": "h2h", "side": "home",
            "price": -115, "recordedAt": "2026-09-30T18:00:00Z", "inPlay": False}
    rows, excluded = normalize_archived_snapshots([base], {"e": "2026010001"})
    assert len(rows) == 1 and not excluded and rows[0]["game_id"] == "2026010001"
    assert normalize_archived_snapshots([{**base, "inPlay": None}], {"e": "2026010001"})[0] == []
    assert normalize_archived_snapshots([base], {})[0] == []
