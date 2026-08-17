"""Generate Kodi/Jellyfin-compatible NFO sidecar files alongside .strm files.

Movies: movies/Title (Year)/Title (Year).nfo
Series: series/Title/tvshow.nfo

Jellyfin reads these to get the exact IMDb ID, so it can fetch metadata and
posters without guessing from the folder name.  When multiple folders exist
for the same series (different torrent sources), writing the same IMDb ID in
all tvshow.nfo files allows Jellyfin to merge them into a single library entry.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as _stdlib_xml_escape

import requests as _requests

import db
import tmdb
from config import MEDIA_PATH
from io_utils import atomic_write_text

_IMAGE_BASE_POSTER = "https://image.tmdb.org/t/p/w500"
_IMAGE_BASE_BACKDROP = "https://image.tmdb.org/t/p/w1280"
_IMAGE_BASE_STILL = "https://image.tmdb.org/t/p/w300"
_EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")

log = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"\((\d{4})\)$")
_SEASON_TRAIL_RE = re.compile(r'\s+(?:S\d{1,2}(?:E\d+)?|Season\s+\d+).*$', re.IGNORECASE)
_YEAR_TRAIL_RE = re.compile(r'\s+\d{4}$')
_PREFIX_RE = re.compile(
    r'^(\[[^\]]+\]\s*|www[\s.][\w.\-]+(?:[\s.][\w.\-]+)*\s*-\s*|www\s+\w+\s+\w+\s*-\s*'
    r'|rutor\.?\s*info\s*|\[?DEVIL-TORRENTS[^\]]*\]?\s*|HIDRATORRENTS[^\s]*\s*(?:MKV)?\s*-?(?:LEGENDADO)?-?\s*'
    r'|superseed\s+\S+\s*|\[BEST-TORRENTS[^\]]*\]\s*|\[XTORRENTY[^\]]*\]\s*)+',
    re.IGNORECASE,
)
_CYRILLIC_PREFIX_RE = re.compile(r'^[Ѐ-ӿ\s\(\)\[\]«»,.\-– - ]+')


def _clean_for_tmdb(raw: str) -> str:
    s = _PREFIX_RE.sub("", raw).strip()
    # Strip leading Cyrillic block (Russian title prepended by torrent sites)
    s = _CYRILLIC_PREFIX_RE.sub("", s).strip()
    # Strip parenthesised blocks containing Cyrillic (e.g. director name)
    s = re.sub(r'\([^)]*[Ѐ-ӿ][^)]*\)', '', s).strip()
    s = _SEASON_TRAIL_RE.sub("", s).strip()
    # Strip trailing year in parens  -  passed separately as year_hint
    s = re.sub(r'\s*\(\d{4}\)\s*$', '', s).strip()
    s = _YEAR_TRAIL_RE.sub("", s).strip()
    s = re.sub(r"[\[\(\{\s\-]+$", "", s).strip()
    return s


def _xml_escape(s: str) -> str:
    """Escape characters that have special meaning in XML element content."""
    return _stdlib_xml_escape(s or "")


def _runtime_minutes(runtime_minutes: int | float | None) -> int | None:
    try:
        minutes = int(round(float(runtime_minutes or 0)))
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


def _runtime_tag(runtime_minutes: int | float | None) -> str:
    """Return a Kodi/Jellyfin runtime tag (whole minutes), or an empty string."""
    minutes = _runtime_minutes(runtime_minutes)
    return f"\n  <runtime>{minutes}</runtime>" if minutes else ""


def _movie_nfo(title: str, year: int | None, imdb_id: str,
               runtime_minutes: int | float | None = None) -> str:
    year_tag = f"\n  <year>{year}</year>" if year else ""
    runtime_tag = _runtime_tag(runtime_minutes)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<movie>\n"
        f"  <title>{_xml_escape(title)}</title>{year_tag}{runtime_tag}\n"
        f'  <uniqueid type="imdb" default="true">{_xml_escape(imdb_id)}</uniqueid>\n'
        "</movie>\n"
    )


def _episode_nfo(title: str, season: int, episode: int,
                 plot: str | None = None, aired: str | None = None,
                 runtime_minutes: int | float | None = None) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<episodedetails>",
        "  <title>%s</title>" % _xml_escape(title),
        "  <season>%d</season>" % season,
        "  <episode>%d</episode>" % episode,
    ]
    if plot:
        lines.append("  <plot>%s</plot>" % _xml_escape(plot))
    if aired:
        lines.append("  <aired>%s</aired>" % _xml_escape(aired))
    runtime_tag = _runtime_tag(runtime_minutes).strip()
    if runtime_tag:
        lines.append("  %s" % runtime_tag)
    lines.append("</episodedetails>")
    return "\n".join(lines) + "\n"


_GENERIC_EPISODE_TITLE_RE = re.compile(r"^Episode\s+\d+$", re.IGNORECASE)


def _positive_runtime(root: ET.Element) -> bool:
    el = root.find("runtime")
    try:
        return bool(el is not None and float(el.text or 0) > 0)
    except (TypeError, ValueError):
        return False


def _episode_nfo_needs_metadata(nfo_path: Path) -> bool:
    if not nfo_path.exists():
        return True
    try:
        root = ET.parse(nfo_path).getroot()
    except Exception:
        return False
    title = (root.findtext("title") or "").strip()
    return not _positive_runtime(root) or not title or bool(_GENERIC_EPISODE_TITLE_RE.match(title))


def _nfo_needs_runtime(nfo_path: Path) -> bool:
    if not nfo_path.exists():
        return False
    try:
        return not _positive_runtime(ET.parse(nfo_path).getroot())
    except Exception:
        return False


def _write_xml_tree(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    atomic_write_text(
        path,
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body + "\n",
    )


def _merge_episode_metadata(nfo_path: Path, title: str, season: int, episode: int,
                            plot: str | None = None, aired: str | None = None,
                            runtime_minutes: int | float | None = None) -> bool:
    """Add trustworthy TMDB metadata without discarding existing stream details."""
    if not nfo_path.exists():
        atomic_write_text(
            nfo_path,
            _episode_nfo(title, season, episode, plot, aired, runtime_minutes),
        )
        return True
    try:
        root = ET.parse(nfo_path).getroot()
    except Exception:
        return False

    changed = False
    current_title = (root.findtext("title") or "").strip()
    if title and (not current_title or _GENERIC_EPISODE_TITLE_RE.match(current_title)):
        el = root.find("title")
        if el is None:
            el = ET.Element("title")
            root.insert(0, el)
        el.text = title
        changed = True

    for tag, value in (("season", season), ("episode", episode),
                       ("plot", plot), ("aired", aired)):
        if value is None or (root.findtext(tag) or "").strip():
            continue
        ET.SubElement(root, tag).text = str(value)
        changed = True

    runtime = _runtime_minutes(runtime_minutes)
    if runtime and not _positive_runtime(root):
        el = root.find("runtime")
        if el is None:
            el = ET.SubElement(root, "runtime")
        el.text = str(runtime)
        changed = True

    if changed:
        _write_xml_tree(nfo_path, root)
    return changed


def _merge_runtime(nfo_path: Path, runtime_minutes: int | float | None) -> bool:
    minutes = _runtime_minutes(runtime_minutes)
    if not nfo_path.exists() or not minutes:
        return False
    try:
        root = ET.parse(nfo_path).getroot()
    except Exception:
        return False
    if _positive_runtime(root):
        return False
    el = root.find("runtime")
    if el is None:
        el = ET.SubElement(root, "runtime")
    el.text = str(minutes)
    _write_xml_tree(nfo_path, root)
    return True


def _tvshow_nfo(title: str, imdb_id: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<tvshow>\n"
        f"  <title>{_xml_escape(title)}</title>\n"
        f'  <uniqueid type="imdb" default="true">{_xml_escape(imdb_id)}</uniqueid>\n'
        "</tvshow>\n"
    )


_BAD_TITLE_RE = re.compile(r'^Season\s+\d+$', re.IGNORECASE)


def repair_tvshow_titles() -> dict:
    """Rewrite tvshow.nfo files whose title is 'Season XX' instead of the real show name.

    Uses (in order of preference):
      1. Canonical title from monitored_series table (by imdb_id)
      2. Folder name stripped of trailing region codes like (IN) or (ZA)
    """
    media = Path(MEDIA_PATH)
    series_dir = media / "series"
    if not series_dir.is_dir():
        return {"fixed": 0, "skipped": 0}

    monitored_by_imdb = {s["imdb_id"]: s["title"] for s in db.get_all_monitored_series()}

    fixed = skipped = 0
    for folder in sorted(series_dir.iterdir()):
        if not folder.is_dir():
            continue
        nfo_path = folder / "tvshow.nfo"
        if not nfo_path.exists():
            continue
        try:
            root = ET.parse(nfo_path).getroot()
        except Exception:
            continue

        title_el = root.find("title")
        if title_el is None or not (title_el.text or "").strip():
            continue
        if not _BAD_TITLE_RE.match(title_el.text.strip()):
            continue  # title already looks correct

        imdb_id = _read_imdb_from_nfo(nfo_path)
        if not imdb_id:
            skipped += 1
            continue

        # Prefer canonical DB title, fall back to folder name (strip region suffix like (IN))
        correct_title = monitored_by_imdb.get(imdb_id)
        if not correct_title or _BAD_TITLE_RE.match(correct_title):
            correct_title = re.sub(r'\s+\([A-Z]{2}\)$', '', folder.name).strip() or folder.name

        try:
            atomic_write_text(nfo_path, _tvshow_nfo(correct_title, imdb_id))
            log.info("NFO repair: '%s' -> '%s' (%s)", title_el.text.strip(), correct_title, nfo_path)
            fixed += 1
        except Exception as exc:
            log.warning("NFO repair: could not write %s: %s", nfo_path, exc)
            skipped += 1

    log.info("NFO repair complete: %d fixed, %d skipped", fixed, skipped)
    return {"fixed": fixed, "skipped": skipped}


def _read_imdb_from_nfo(nfo_path: Path) -> str | None:
    """Parse IMDb ID from a Kodi/Jellyfin .nfo file."""
    try:
        root = ET.parse(nfo_path).getroot()
        for uid in root.findall("uniqueid"):
            if uid.get("type") == "imdb" and uid.text:
                return uid.text.strip()
    except Exception:
        pass
    return None


def _download_image(url: str, dest: Path) -> bool:
    try:
        resp = _requests.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(65536):
                fh.write(chunk)
        log.info("Saved image: %s", dest)
        return True
    except Exception as exc:
        log.debug("Image download failed %s: %s", dest.name, exc)
        dest.unlink(missing_ok=True)
        return False


def _write(path: Path, content: str) -> bool:
    if path.exists():
        return False
    try:
        atomic_write_text(path, content)
        log.info("Wrote NFO: %s", path)
        return True
    except Exception as exc:
        log.warning("Could not write NFO %s: %s", path, exc)
        return False


def generate_all(*, lookup_missing: bool = True) -> dict:
    """Write missing NFO files for all movies and series using IMDb IDs from DB.

    For series folders not found in the DB (messy torrent names), a TMDB lookup
    is attempted so that duplicate series folders get the same IMDb ID in their
    tvshow.nfo  -  Jellyfin then merges them into a single library entry. Routine
    refreshes can disable those broad lookups so unresolved legacy folders do not
    consume TMDB quota and CPU again after every TorBox event.
    """
    media = Path(MEDIA_PATH)
    items_by_title = {m["title"]: m["imdb_id"] for m in db.get_media_items()}
    # Secondary lookup: imdb_id → title (canonical)
    monitored_by_imdb = {s["imdb_id"]: s["title"] for s in db.get_all_monitored_series()}

    movies = series = 0

    movies_dir = media / "movies"
    if movies_dir.is_dir():
        for folder in movies_dir.iterdir():
            if not folder.is_dir():
                continue
            nfo_path = folder / f"{folder.name}.nfo"
            if nfo_path.exists():
                continue
            m_yr = _YEAR_RE.search(folder.name)
            year_hint = int(m_yr.group(1)) if m_yr else None
            clean = _clean_for_tmdb(folder.name)

            # DB lookup: try folder name as-is, then without year, then cleaned title
            imdb_id = (
                items_by_title.get(folder.name)
                or (items_by_title.get(clean) if clean else None)
                or (items_by_title.get(f"{clean} ({year_hint})") if clean and year_hint else None)
            )
            if imdb_id and imdb_id.startswith("unknown_"):
                imdb_id = None

            if not imdb_id and not lookup_missing:
                continue
            if not imdb_id:
                if not clean:
                    continue
                try:
                    imdb_id = tmdb.search_movie(clean, year_hint)
                    time.sleep(0.2)
                except Exception:
                    imdb_id = None
                if not imdb_id:
                    # Retry without year hint  -  some films have wrong year in folder name
                    try:
                        imdb_id = tmdb.search_movie(clean, None)
                        time.sleep(0.2)
                    except Exception:
                        imdb_id = None
                if not imdb_id:
                    continue
            m = _YEAR_RE.search(folder.name)
            year = int(m.group(1)) if m else None
            title = _YEAR_RE.sub("", folder.name).strip() if m else folder.name
            if _write(nfo_path, _movie_nfo(title, year, imdb_id)):
                movies += 1

    series_dir = media / "series"
    if series_dir.is_dir():
        for folder in series_dir.iterdir():
            if not folder.is_dir():
                continue
            nfo_path = folder / "tvshow.nfo"
            if nfo_path.exists():
                continue

            imdb_id = items_by_title.get(folder.name)
            if (not imdb_id or imdb_id.startswith("unknown_")) and not lookup_missing:
                continue
            if not imdb_id or imdb_id.startswith("unknown_"):
                # Not in DB  -  try TMDB lookup so duplicate folders get the right ID
                clean = _clean_for_tmdb(folder.name)
                if not clean:
                    continue
                try:
                    imdb_id = tmdb.search_tv(clean)
                    time.sleep(0.2)
                except Exception:
                    imdb_id = None
                if not imdb_id:
                    continue

            # Use canonical title from monitored_series if available
            display_title = monitored_by_imdb.get(imdb_id, _clean_for_tmdb(folder.name) or folder.name)
            # Safety guard: never write a "Season XX" string as the show title
            if _BAD_TITLE_RE.match(display_title):
                display_title = folder.name
            if _write(nfo_path, _tvshow_nfo(display_title, imdb_id)):
                series += 1

    log.info("NFO generation complete: %d movie(s), %d series", movies, series)
    return {"movies": movies, "series": series}


def fetch_images_for_folder(folder: Path, imdb_id: str, media_type: str = "movie") -> None:
    """Download poster.jpg + fanart.jpg for a single folder. Called atomically at creation."""
    poster = folder / "poster.jpg"
    fanart = folder / "fanart.jpg"
    if not (poster.exists() and fanart.exists()):
        try:
            p, b = tmdb.get_images(imdb_id, "movie" if media_type == "movie" else "tv")
            if p and not poster.exists():
                _download_image(f"{_IMAGE_BASE_POSTER}{p}", poster)
            if b and not fanart.exists():
                _download_image(f"{_IMAGE_BASE_BACKDROP}{b}", fanart)
        except Exception as exc:
            log.debug("fetch_images_for_folder %s: %s", folder.name, exc)
    if media_type == "movie":
        nfo_path = folder / f"{folder.name}.nfo"
        try:
            runtime_sec = tmdb.get_movie_runtime_sec(imdb_id)
            if runtime_sec:
                _merge_runtime(nfo_path, runtime_sec / 60.0)
        except Exception as exc:
            log.debug("movie runtime %s: %s", folder.name, exc)
    else:
        try:
            tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
            if tmdb_id:
                _write_episode_meta(folder, tmdb_id)
        except Exception as exc:
            log.debug("episode meta %s: %s", folder.name, exc)


def _write_episode_meta(folder: Path, tmdb_id: int) -> tuple[int, int]:
    """Write missing episode .nfo (title/plot/aired) + -thumb.jpg for a series folder.

    One TMDB call per SEASON (not per episode), so the library's many bogus
    episode numbers are skipped with no 404 storm. Jellyfin AND Plex read episode
    titles locally with no internet metadata fetching."""
    n_nfo = n_still = 0
    for season_folder in sorted(folder.iterdir()):
        if not season_folder.is_dir():
            continue
        # group episodes still needing meta by real season number (from filename)
        by_season: dict = {}
        for strm in sorted(season_folder.glob("*.strm")):
            m = _EP_RE.search(strm.name)
            if not m:
                continue
            nfo = strm.with_suffix(".nfo")
            thumb = strm.with_name(f"{strm.stem}-thumb.jpg")
            if not _episode_nfo_needs_metadata(nfo) and thumb.exists():
                continue
            by_season.setdefault(int(m.group(1)), []).append(
                (int(m.group(2)), nfo, thumb))
        for s_num, items in by_season.items():
            try:
                eps = {e.get("episode_number"): e
                       for e in tmdb.get_season_episodes(tmdb_id, s_num)}
                time.sleep(0.15)
            except Exception:
                continue
            for e_num, nfo, thumb in items:
                det = eps.get(e_num)
                if not det:
                    continue
                if det.get("name") and _episode_nfo_needs_metadata(nfo):
                    try:
                        if _merge_episode_metadata(
                            nfo,
                            det["name"],
                            s_num,
                            e_num,
                            det.get("overview"),
                            det.get("air_date"),
                            det.get("runtime"),
                        ):
                            n_nfo += 1
                    except Exception as exc:
                        log.debug("episode nfo write failed %s: %s", nfo.name, exc)
                still = det.get("still_path")
                if still and not thumb.exists():
                    if _download_image(f"{_IMAGE_BASE_STILL}{still}", thumb):
                        n_still += 1
    return n_nfo, n_still


def fetch_local_images() -> dict:
    """Download poster.jpg, fanart.jpg, and episode stills from TMDB for all media folders.

    Skips files that already exist so re-runs are cheap. Sleeps 150 ms between
    TMDB calls to stay well under the 50 req/s rate limit.
    """
    media = Path(MEDIA_PATH)
    m_count = s_count = e_count = metadata_count = 0

    movies_dir = media / "movies"
    if movies_dir.is_dir():
        for folder in sorted(movies_dir.iterdir()):
            if not folder.is_dir():
                continue
            poster = folder / "poster.jpg"
            fanart = folder / "fanart.jpg"
            nfo = folder / f"{folder.name}.nfo"
            if not nfo.exists():
                continue
            needs_runtime = _nfo_needs_runtime(nfo)
            if poster.exists() and fanart.exists() and not needs_runtime:
                continue
            imdb_id = _read_imdb_from_nfo(nfo)
            if not imdb_id:
                continue
            if needs_runtime:
                try:
                    runtime_sec = tmdb.get_movie_runtime_sec(imdb_id)
                    time.sleep(0.15)
                    if runtime_sec and _merge_runtime(nfo, runtime_sec / 60.0):
                        metadata_count += 1
                except Exception:
                    pass
            if poster.exists() and fanart.exists():
                continue
            try:
                p, b = tmdb.get_images(imdb_id, "movie")
                time.sleep(0.15)
            except Exception:
                continue
            if p and not poster.exists():
                if _download_image(f"{_IMAGE_BASE_POSTER}{p}", poster):
                    m_count += 1
            if b and not fanart.exists():
                _download_image(f"{_IMAGE_BASE_BACKDROP}{b}", fanart)

    series_dir = media / "series"
    if series_dir.is_dir():
        for folder in sorted(series_dir.iterdir()):
            if not folder.is_dir():
                continue
            nfo = folder / "tvshow.nfo"
            imdb_id = _read_imdb_from_nfo(nfo) if nfo.exists() else None

            if imdb_id:
                poster = folder / "poster.jpg"
                fanart = folder / "fanart.jpg"
                if not poster.exists() or not fanart.exists():
                    try:
                        p, b = tmdb.get_images(imdb_id, "tv")
                        time.sleep(0.15)
                        if p and not poster.exists():
                            if _download_image(f"{_IMAGE_BASE_POSTER}{p}", poster):
                                s_count += 1
                        if b and not fanart.exists():
                            _download_image(f"{_IMAGE_BASE_BACKDROP}{b}", fanart)
                    except Exception:
                        pass

                # Episode NFOs + stills  -  one TMDB call per episode, reused for both
                tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
                if tmdb_id:
                    time.sleep(0.15)
                    try:
                        _n_nfo, _n_still = _write_episode_meta(folder, tmdb_id)
                        metadata_count += _n_nfo
                        e_count += _n_still
                    except Exception as exc:
                        log.debug("episode meta backfill %s: %s", folder.name, exc)

    log.info("fetch_local_images: %d movie poster(s), %d series poster(s), "
             "%d episode still(s), %d metadata update(s)",
             m_count, s_count, e_count, metadata_count)
    return {"movies": m_count, "series": s_count, "episodes": e_count,
            "metadata": metadata_count}
