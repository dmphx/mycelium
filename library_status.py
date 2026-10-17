"""Read-only library presence shared by discovery, requests and bot clients.

A processing attempt can fail after a title has entered the library. Indexed
stream entries take precedence over that attempt; an unindexed resolver row
does not. Presence is not a guarantee that an upstream stream is playable now.
"""
def media_kind(value):
    return "tv" if value == "series" else value


def by_imdb(imdb_ids):
    """Return {(media_type, imdb_id): status}, without changing attempt history."""
    import db
    ids = list({i for i in imdb_ids if i})
    result = {}
    with db._connect() as conn:
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            ph = ",".join("?" for _ in batch)
            for row in conn.execute(
                f"SELECT imdb_id, media_type, status FROM requests WHERE imdb_id IN ({ph})",
                batch,
            ):
                result[(media_kind(row["media_type"]), row["imdb_id"])] = row["status"]
            # Include media_type to use the existing compound identity index.
            for row in conn.execute(
                f"""SELECT DISTINCT imdb_id, media_type FROM virtual_items
                    WHERE media_type IN ('movie', 'series', 'tv')
                      AND imdb_id IN ({ph}) AND strm_path IS NOT NULL AND strm_path <> ''""",
                batch,
            ):
                result[(media_kind(row["media_type"]), row["imdb_id"])] = "success"
    return result


_PRIORITY = {"success": 0, "available": 0, "wanted": 1, "pending": 2,
             "upcoming": 3, "failed": 4}


def by_tmdb(tmdb_ids=None):
    """Return typed TMDB identities and effective statuses from durable links."""
    import db
    if tmdb_ids is not None:
        tmdb_ids = list({i for i in tmdb_ids if i})
        if not tmdb_ids:
            return {}
    clauses, args = [], []
    for table, alias, kind, status in (
        ("requests", "r", "r.media_type", "r.status"),
        ("watchlist", "w", "w.media_type", "r.status"),
        ("monitored_series", "ms", "'tv'", "r.status"),
        ("user_requests", "ur", "ur.media_type", "COALESCE(r.status, ur.status)"),
    ):
        join = "" if alias == "r" else f" LEFT JOIN requests r ON r.imdb_id={alias}.imdb_id"
        where = f"{alias}.tmdb_id IS NOT NULL"
        if tmdb_ids is not None:
            where += f" AND {alias}.tmdb_id IN ({','.join('?' for _ in tmdb_ids)})"
            args.extend(tmdb_ids)
        clauses.append(f"SELECT {alias}.tmdb_id, {alias}.imdb_id, {kind} AS media_type, "
                       f"{status} AS status FROM {table} {alias}{join} WHERE {where}")
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute(" UNION ALL ".join(clauses), args)]
    statuses = by_imdb(r["imdb_id"] for r in rows)
    identities, result = {}, {}
    for row in rows:
        row["media_type"] = media_kind(row["media_type"])
        key = (row["media_type"], row["tmdb_id"])
        if row["imdb_id"]:
            identities.setdefault(key, set()).add(row["imdb_id"])
        row["status"] = statuses.get((row["media_type"], row["imdb_id"]), row["status"])
        previous = result.get(key)
        if previous is None or _PRIORITY.get(row["status"], 9) < _PRIORITY.get(previous["status"], 9):
            result[key] = row
    # Conflicting identities must not borrow another title's availability.
    return {key: row for key, row in result.items() if len(identities.get(key, ())) <= 1}


def enrich(items):
    """Set library_status on result dictionaries; resolve missing IMDb IDs by type."""
    tmdb_map = by_tmdb(it.get("tmdb_id") for it in items if not it.get("imdb_id"))
    statuses = by_imdb(it.get("imdb_id") for it in items)
    for item in items:
        kind = media_kind(item.get("media_type"))
        if item.get("imdb_id"):
            item["library_status"] = statuses.get((kind, item["imdb_id"]))
        else:
            match = tmdb_map.get((kind, item.get("tmdb_id")), {})
            item["library_status"] = match.get("status")
            if match.get("imdb_id"):
                item["imdb_id"] = match["imdb_id"]
