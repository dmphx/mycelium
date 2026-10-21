#!/usr/bin/env python3
"""Offline, reviewed linking of OIDC identities to EXISTING native users.

Reads a credential-free JSON manifest and a SQLite database file. It never
imports the application, never creates users or moves owner rows, and only
inserts rows into external_identities. Default is a check (read-only dry run)
that prints a plan fingerprint. --apply needs that same fingerprint and runs
the whole batch in one transaction, rolling back on any drift or conflict.

Manifest (version 1):
{"version": 1, "links": [{
  "native_user_id": 7,
  "jellyfin": {"server_guid": "<32 hex>", "user_guid": "<32 hex>"},
  "oidc": {"issuer": "<exact issuer string>", "subject": "<exact sub>"},
  "expected": {"role": "user", "enabled": 1, "quota_monthly": 0, "auto_approve": 0},
  "existing_links": [{"provider": "jellyfin", "server_id": "...", "subject": "..."}]
}]}
existing_links is the exact set of links the user holds today, ignoring any
oidc link for the same issuer (so a re-run after apply stays a no-op).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sqlite3
import sys

HEX32 = re.compile(r"^[0-9a-f]{32}$")
ROLES = ("user", "admin")
EXPECTED_KEYS = ("role", "enabled", "quota_monthly", "auto_approve")


class PlanError(Exception):
    pass


def _norm_guid(value, label):
    if not isinstance(value, str):
        raise PlanError(f"{label} is not a 32 hex GUID")
    v = value.replace("-", "").lower()
    if not HEX32.match(v):
        raise PlanError(f"{label} is not a 32 hex GUID")
    return v


def _no_dupes(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise PlanError("manifest has duplicate JSON keys")
    return dict(pairs)


def _no_const(name):
    raise PlanError(f"manifest has non-finite number {name}")


def load_manifest(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh, object_pairs_hook=_no_dupes, parse_constant=_no_const)
    except json.JSONDecodeError as exc:
        raise PlanError(f"manifest is not valid JSON: {exc.msg}") from None
    return normalize_manifest(data)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _strict_expected(exp):
    if not isinstance(exp, dict) or set(exp) != set(EXPECTED_KEYS):
        raise PlanError("expected must hold exactly " + ", ".join(EXPECTED_KEYS))
    if exp["role"] not in ROLES:
        raise PlanError("expected role must be one of " + ", ".join(ROLES))
    for k in ("enabled", "auto_approve"):
        if not _is_int(exp[k]) or exp[k] not in (0, 1):
            raise PlanError(f"expected {k} must be exactly 0 or 1")
    if not _is_int(exp["quota_monthly"]) or exp["quota_monthly"] < 0:
        raise PlanError("expected quota_monthly must be a non-negative integer")
    return {k: exp[k] for k in EXPECTED_KEYS}


def _strict_str(v, label):
    if not isinstance(v, str) or not v:
        raise PlanError(f"{label} must be a non-empty string")
    return v


def normalize_manifest(data):
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or not isinstance(data.get("links"), list)):
        raise PlanError("manifest must be version 1 with a links list")
    out = []
    for i, link in enumerate(data["links"]):
        try:
            if not isinstance(link, dict):
                raise PlanError("link must be an object")
            uid = link["native_user_id"]
            if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
                raise PlanError("native_user_id must be a positive integer")
            jf = link["jellyfin"]
            oidc = link["oidc"]
            iss, sub = oidc["issuer"], oidc["subject"]
            if not isinstance(iss, str) or not iss or iss != iss.strip():
                raise PlanError("oidc issuer must be a non-empty exact string")
            if not isinstance(sub, str) or not sub or len(sub) > 512:
                raise PlanError("oidc subject must be a non-empty string")
            exp = _strict_expected(link["expected"])
            if not isinstance(link["existing_links"], list):
                raise PlanError("existing_links must be a list")
            existing = sorted(
                ({"provider": _strict_str(e["provider"], "provider"),
                  "server_id": _strict_str(e["server_id"], "server_id"),
                  "subject": _strict_str(e["subject"], "subject")} for e in link["existing_links"]),
                key=lambda e: (e["provider"], e["server_id"], e["subject"]))
            jf_norm = {"server_guid": _norm_guid(jf["server_guid"], "server_guid"),
                       "user_guid": _norm_guid(jf["user_guid"], "user_guid")}
        except PlanError as exc:
            raise PlanError(f"link {i}: {exc}") from None
        except (KeyError, TypeError, AttributeError) as exc:
            raise PlanError(f"link {i}: missing or malformed field {exc}") from None
        out.append({
            "native_user_id": uid,
            "jellyfin": jf_norm,
            "oidc": {"issuer": iss, "subject": sub},
            "expected": exp,
            "existing_links": existing,
        })
    return {"version": 1, "links": out}


def _observe(conn, link):
    """Read the current preimage for one link and raise PlanError on any drift."""
    uid = link["native_user_id"]
    row = conn.execute(
        "SELECT id, role, enabled, quota_monthly, auto_approve FROM users WHERE id=?", (uid,)
    ).fetchone()
    if row is None:
        raise PlanError(f"user {uid} does not exist")
    actual = {k: row[k] for k in EXPECTED_KEYS}
    if actual != link["expected"]:
        raise PlanError(f"user {uid} policy drifted from the manifest")
    jf = link["jellyfin"]
    owner = conn.execute(
        "SELECT user_id FROM external_identities WHERE provider='jellyfin' AND server_id=? AND subject=?",
        (jf["server_guid"], jf["user_guid"])).fetchone()
    if owner is None or int(owner["user_id"]) != uid:
        raise PlanError(f"user {uid} is not anchored to the stated Jellyfin identity")
    iss, sub = link["oidc"]["issuer"], link["oidc"]["subject"]
    links = [dict(r) for r in conn.execute(
        "SELECT provider, server_id, subject FROM external_identities WHERE user_id=? "
        "ORDER BY provider, server_id, subject", (uid,))]
    others = [e for e in links if not (e["provider"] == "oidc" and e["server_id"] == iss)]
    if others != link["existing_links"]:
        raise PlanError(f"user {uid} existing links differ from the manifest")
    mine = [e for e in links if e["provider"] == "oidc" and e["server_id"] == iss]
    if mine and mine[0]["subject"] != sub:
        raise PlanError(f"user {uid} already has a different subject for this issuer")
    holder = conn.execute(
        "SELECT user_id FROM external_identities WHERE provider='oidc' AND server_id=? AND subject=?",
        (iss, sub)).fetchone()
    if holder is not None and int(holder["user_id"]) != uid:
        raise PlanError("subject is already linked to another user")
    return {"user": uid, "policy": actual, "links": others, "already_linked": bool(mine)}


def build_plan(conn, manifest):
    seen_users, seen_subjects = set(), set()
    steps = []
    for link in manifest["links"]:
        uid = link["native_user_id"]
        key = (link["oidc"]["issuer"], link["oidc"]["subject"])
        if uid in seen_users or key in seen_subjects:
            raise PlanError("manifest repeats a user or an OIDC identity")
        seen_users.add(uid)
        seen_subjects.add(key)
        steps.append({"link": link, "observed": _observe(conn, link)})
    canonical = json.dumps({"manifest": manifest, "observed": [s["observed"] for s in steps]},
                           sort_keys=True, separators=(",", ":"))
    return steps, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _open(base, mode):
    try:
        conn = sqlite3.connect(f"{base}?mode={mode}", uri=True, isolation_level=None, timeout=30)
    except sqlite3.Error as exc:
        raise PlanError(f"cannot open database: {type(exc).__name__}") from None
    conn.row_factory = sqlite3.Row
    return conn


def run(db_path, manifest, apply=False, fingerprint=None):
    """Return a result dict; raises PlanError on drift or conflict."""
    if apply and not fingerprint:
        raise PlanError("--apply requires --fingerprint from a prior check")
    base = pathlib.Path(db_path).resolve().as_uri()
    mode = "rw" if apply else "ro"
    conn = _open(base, mode)
    added = 0
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        # One explicit transaction either way: a coherent read snapshot for the
        # check, the write lock for apply.
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            steps, fp = build_plan(conn, manifest)
            if apply and fp != fingerprint:
                raise PlanError("plan fingerprint does not match: database or manifest drifted")
            if apply:
                for s in steps:
                    if s["observed"]["already_linked"]:
                        continue
                    o = s["link"]["oidc"]
                    conn.execute(
                        "INSERT INTO external_identities (provider, server_id, subject, user_id, display_name) "
                        "VALUES ('oidc', ?, ?, ?, '')",
                        (o["issuer"], o["subject"], s["link"]["native_user_id"]))
                    added += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.IntegrityError as exc:
        raise PlanError("link conflict rolled back the whole batch") from exc
    except sqlite3.Error as exc:
        raise PlanError(f"database error: {type(exc).__name__}: {exc}") from None
    finally:
        conn.close()
    pending = sum(1 for s in steps if not s["observed"]["already_linked"])
    return {"fingerprint": fp, "links": len(steps), "to_add": 0 if apply else pending,
            "added": added, "applied": bool(apply)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--apply", action="store_true", help="write; default is a read-only check")
    ap.add_argument("--fingerprint")
    a = ap.parse_args(argv)
    try:
        res = run(a.db, load_manifest(a.manifest), apply=a.apply, fingerprint=a.fingerprint)
    except (PlanError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(res, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
