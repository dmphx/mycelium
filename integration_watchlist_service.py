"""Owned watchlist mutations with retained historical results.

No automatic requests, grants or provider startup. New membership
uses checked typed detail and IMDb proof, then commits native membership, complete
metadata selection and its immutable result in one transaction. Replays are reads.
"""
import integration_auth as ia
import integration_catalog as ic
import integration_catalog_service as catalog
import integration_personalization as personalization
import integration_watchlist_overlay as overlay
import tmdb_checked as tc


def operations(user_id, *, metadata_ready=False):
    supported = set()
    try:
        if not overlay.check_selection(user_id)["ready"]:
            return supported
        supported.add("watchlist.read")
        if metadata_ready:
            with overlay._transaction() as conn:
                overlay._enabled(conn, user_id)
                personalization._ledger_health(conn)
                supported.add("watchlist.write")
    except ia.IntegrationError:
        pass
    return supported


def _resolve(kind, tid, fetch):
    catalog._require_key(catalog._read_setting)
    try:
        raw = (fetch or tc.fetch)(f"/{kind}/{tid}", {"append_to_response": "external_ids"})
    except tc.TmdbNotFound:
        overlay._fail()
    except tc.TmdbUnavailable as error:
        overlay._fail(retryable=str(error) in catalog.RETRYABLE_REASONS)
    except Exception:
        overlay._fail()
    if type(raw) is not dict or type(raw.get("id")) is not int or raw["id"] != tid \
            or type(raw.get("overview")) is not str:
        overlay._fail()
    external = raw.get("external_ids")
    if type(external) is not dict or type(external.get("imdb_id")) is not str \
            or not overlay._IMDB.fullmatch(external["imdb_id"]) \
            or "id" in external and (type(external["id"]) is not int or external["id"] != tid) \
            or raw.get("imdb_id") is not None and raw["imdb_id"] != external["imdb_id"]:
        overlay._fail()
    try:
        title = ic.title(raw, kind)
        overlay._title(title, kind, tid)
    except ic.CatalogUnavailable:
        overlay._fail()
    return {"media_type": kind, "tmdb_id": tid, "imdb_id": external["imdb_id"],
            "title": title, "title_hash": overlay._hash("title", title),
            "identity_proof": {"media_type": kind, "detail_id": tid,
                "external_parent_id": external.get("id"), "external_imdb_id": external["imdb_id"],
                "detail_hash": overlay._hash("detail-response", raw),
                "find_candidate_id": None, "find_hash": None}}


def _document(conn, owner, entries, new_row=None):
    rows, identity_hash, source_hash = overlay._snapshot(conn, owner)
    if {r["id"] for r in rows} != set(entries):
        overlay._fail("watchlist_source_changed", 409)
    built = []
    for row in rows:
        entry = entries[row["id"]]
        row_hash = overlay._hash("source-row", row)
        if row["id"] != new_row and row_hash != entry["row_source_hash"]:
            overlay._fail("watchlist_source_changed", 409)
        built.append({**entry, "watchlist_id": row["id"], "row_source_hash": row_hash,
                      "native_identity": overlay._identity(row, owner)})
    document = {"format_version": 1, "user_id": owner, "identity_set_hash": identity_hash,
                "source_snapshot_hash": source_hash, "row_count": len(rows), "entries": built}
    document["plan_hash"] = overlay._hash("plan", document)
    return overlay._validate(document)


def mutate(user_id, action, payload, key, body_sha256, *, fetch=None):
    """Exact media-only body. Caller must already have verified the native actor.

    A historical result does not reapply its membership change. New work needs an
    explicit current complete selection; no implicit migration or metadata repair.
    All provider work is outside SQLite transactions and is followed by full CAS.
    """
    semantic = personalization._binding("watchlist", action, payload, key)
    if type(body_sha256) is not str or not personalization._HASH.fullmatch(body_sha256):
        personalization._bad()
    kind, tid = payload["media"]["type"], payload["media"]["tmdb_id"]
    identity = {"media": {"type": kind, "tmdb_id": tid}}
    with overlay._transaction() as conn:
        overlay._enabled(conn, user_id)
        replay = personalization._result(conn, user_id, "watchlist", action, identity, key, semantic)
        if replay is not None:
            return replay
        personalization._ledger_health(conn)
        personalization._capacity(conn, user_id)
        initial, document = overlay._selected_document(conn, user_id)
        if initial is None:
            overlay._fail("watchlist_not_selected")
        existing = next((e for e in document["entries"] if (e["media_type"], e["tmdb_id"]) == (kind, tid)), None)
        if action == "POST" and existing is None:
            if document["row_count"] >= overlay.MAX_NATIVE_ROWS:
                overlay._fail()
            overlay._retention(conn, user_id, document["row_count"] + 1)
    resolved = _resolve(kind, tid, fetch) if action == "POST" and existing is None else None
    with overlay._transaction(write=True) as conn:
        overlay._enabled(conn, user_id)
        replay = personalization._result(conn, user_id, "watchlist", action, identity, key, semantic)
        if replay is not None:
            return replay
        personalization._ledger_health(conn)
        personalization._capacity(conn, user_id)
        selection, current = overlay._selected_document(conn, user_id)
        if selection != initial:
            overlay._fail("watchlist_selection_changed", 409)
        entries = {e["watchlist_id"]: e for e in current["entries"]}
        new_row = None
        if resolved is not None:
            if conn.execute("SELECT 1 FROM watchlist WHERE user_id=? AND imdb_id=? AND media_type=?",
                            (user_id, resolved["imdb_id"], kind)).fetchone() is not None:
                overlay._fail("watchlist_identity_conflict", 409)
            poster = resolved["title"].get("poster_url")
            poster_path = None if poster is None else poster.removeprefix("https://image.tmdb.org/t/p/w500")
            inserted = conn.execute("INSERT INTO watchlist(user_id,imdb_id,tmdb_id,media_type,title,poster_path) "
                                    "VALUES (?,?,?,?,?,?)",
                                    (user_id, resolved["imdb_id"], tid, kind, resolved["title"]["title"], poster_path))
            new_row = inserted.lastrowid
            entries[new_row] = resolved
        elif action == "DELETE" and existing is not None:
            removed = conn.execute("DELETE FROM watchlist WHERE user_id=? AND id=?",
                                   (user_id, existing["watchlist_id"]))
            if removed.rowcount != 1:
                overlay._fail("watchlist_source_changed", 409)
            del entries[existing["watchlist_id"]]
        prepared = _document(conn, user_id, entries, new_row)
        overlay._install_document(conn, prepared)
        overlay._select_document(conn, prepared, initial["revision"])
        data = {"media": identity["media"], "saved": action == "POST"}
        conn.execute("""INSERT INTO integration_personalization_results
                     (user_id,idem_key,resource,action,identity_json,semantic_hash,
                      raw_body_sha256,response_status,response_json)
                     VALUES (?,?,'watchlist',?,?,?,?,200,?)""",
                     (user_id, key, action, personalization._json(identity), semantic, body_sha256,
                      personalization._json(data)))
        return data
