"""Shared wire vectors. Runs only when MYCELIUM_WIRE_VECTORS points at the fixture."""
import base64
import hashlib
import json
import os
import sqlite3

import pytest

import integration_auth as ia

PATH = os.environ.get("MYCELIUM_WIRE_VECTORS")
EXPECTED_SHA = "b621804cb1d182805f9d1df7c693f77a5ef154a23c694e55ba83b9a6fe223162"

pytestmark = pytest.mark.skipif(not PATH, reason="MYCELIUM_WIRE_VECTORS not set")


@pytest.fixture(scope="module")
def fixture():
    raw = open(PATH, "rb").read()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA
    return json.loads(raw)


def required_scope(method, target):
    path = target.split("?")[0]
    if path in ("/integration/v1/requests", "/integration/v1/issues"):
        name = path.rsplit("/", 1)[1]
        return f"{name}:write" if method != "GET" else f"{name}:read"
    return "catalog:read"


def test_canonical_vectors(fixture):
    cases = fixture["canonical_cases"]
    assert len(cases) == 34
    for case in cases:
        try:
            normalized = ia.canonical_target(case["raw"], None, strict_raw=False)
        except ia.IntegrationError:
            normalized = None
        assert (normalized is not None) == case["normalize_valid"], case["id"]
        assert normalized == case["canonical"], case["id"]
        try:
            ia.canonical_target(case["raw"], None, strict_raw=True)
            wire = True
        except ia.IntegrationError:
            wire = False
        assert wire == case["wire_valid"], case["id"]


def test_jwt_vectors_with_fresh_replay_each(fixture, tmp_path):
    viewer, service = ia.build_keyrings(fixture["viewer_jwks"], fixture["service_jwks"])
    settings = ia.Settings(fixture["issuer"], fixture["server_guid"], viewer, service)
    linked = fixture["linked_viewer_guid"]
    user = {"id": 7, "enabled": 1}
    cases = fixture["jwt_cases"]
    assert len(cases) == 30
    accepted = rejected = 0
    for i, case in enumerate(cases):
        path = str(tmp_path / f"replay-{i}.db")
        con = sqlite3.connect(path, isolation_level=None)
        ia.init_schema(con)
        con.close()
        headers = [("Authorization", "Bearer " + case["token"])]
        if case["method"] in ia.WRITE_METHODS:
            headers += [("Content-Type", "application/json"), ("Idempotency-Key", case["idempotency_key"])]
        target = case["raw_target"]
        keys = frozenset(["page", "q", "type"]) if target.startswith("/integration/v1/catalog/search") \
            else frozenset()
        try:
            ia.verify_request(
                role=case["keyring"], method=case["method"], raw_target=target,
                body=base64.b64decode(case["body_base64"]), headers=headers,
                required_scope=required_scope(case["method"], target), allowed_query_keys=keys,
                settings=settings, replay=ia.ReplayStore(path=path), now=fixture["fixed_clock_epoch"],
                resolve_user=lambda p, s, sub: user if (p, s, sub) == ("jellyfin", fixture["server_guid"], linked) else None)
            verdict = True
        except ia.IntegrationError:
            verdict = False
        assert verdict == case["accepted"], case["id"]
        accepted += verdict
        rejected += not verdict
    print(f"EXECUTED jwt accepted={accepted} rejected={rejected}")
    assert accepted + rejected == 30
