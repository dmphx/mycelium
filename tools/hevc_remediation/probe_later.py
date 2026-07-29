#!/usr/bin/env python3
"""Post-cooldown proactive probe+fix for the residual Spore stubs whose real codec
could NOT be confirmed from the TorBox release name (untagged / conflict / not_in_mylist).

For each token: materialize the CDN URL, ffprobe the REAL video codec, and if the
on-disk stub declares the wrong codec, rebuild it with the real one (+chown 99:100).
Fixed items are appended to plex_analyze_queue.txt for a Plex re-read.

Resumable (state file) and throttled to stay under TorBox createtorrent limits
(60/hr, 10/min). Designed to be run repeatedly (e.g. hourly) until drained.

Usage: probe_later.py [BATCH=40] [SLEEP_SEC=90]
"""
import json, os, time, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/app")  # resolve mycelium app modules regardless of invocation cwd
import catbox, db, strm_generator as sg, torbox
import mp4_faststart, config as cfg  # Onyx 2026-07-18: hvcC extraction
mp4_faststart.init(cfg.SPORE_MEDIA_PATH + "/.fsh")
# --- Onyx playback-aware throttle: pause remediation while anyone streams ---
# 2026-07-28 gate v2. The old gate keyed ONLY on virtual_items.last_played,
# which is written on a *successful* spore/catbox read (debounced 60s). It
# FREEZES during a buffering stall and lags a cold start -- exactly when a
# viewer is most fragile -- so the gate went blind and this drain 429-stormed
# TorBox mid-episode (The Nanny S04E04, 2026-07-28). Fix: also consult Plex's
# live session list, which reports playing/paused/BUFFERING sessions regardless
# of byte flow, and widen the db fallback window.
_PLAYBACK_WINDOW_S = 600    # db fallback: any title played within 10 min = "watching"
_PLAYBACK_PAUSE_S = 60      # recheck cadence while paused

def _db_recent_play():
    # last_played within the window. Fail-open (return False) on db error.
    import sqlite3
    try:
        c = sqlite3.connect("/data/requests.db", timeout=5)
        try:
            n = c.execute(
                "select count(*) from virtual_items "
                "where last_played > datetime('now', ?)",
                ("-%d seconds" % _PLAYBACK_WINDOW_S,),
            ).fetchone()[0]
        finally:
            c.close()
        return n > 0
    except Exception:
        return False

def _plex_playing():
    # Live Plex sessions survive a buffering stall (state=buffering still lists)
    # and appear the instant playback starts -- the stall-proof signal the db
    # column cannot give. Creds are the same PLEX_URL/PLEX_TOKEN app.py's rescan
    # uses (env, else the settings table). On ANY error return False and let the
    # db signal decide, rather than wedging the drain forever.
    try:
        import os as _os, requests as _rq
        import xml.etree.ElementTree as _ET
        url = _os.environ.get("PLEX_URL", "") or ""
        tok = _os.environ.get("PLEX_TOKEN", "") or ""
        if not (url and tok):
            try:
                import settings as _st
                url = url or (_st.get("PLEX_URL") or "")
                tok = tok or (_st.get("PLEX_TOKEN") or "")
            except Exception:
                pass
        if not (url and tok):
            return False
        r = _rq.get(url.rstrip("/") + "/status/sessions",
                    headers={"X-Plex-Token": tok}, timeout=8)
        if r.status_code != 200:
            return False
        root = _ET.fromstring(r.content)
        # any session (playing OR paused OR buffering) = a viewer is present;
        # do not compete for that title's TorBox byte-rate bucket.
        return int(root.get("size") or 0) > 0
    except Exception:
        return False

def _someone_watching():
    # Robust OR of the two signals; either True -> pause remediation.
    return _plex_playing() or _db_recent_play()


OUT="/data/hevc_remediation"
TOKENS=json.load(open(f"{OUT}/probe_later.json"))
STATE_F=f"{OUT}/probe_later_state.json"
state=json.load(open(STATE_F)) if os.path.exists(STATE_F) else {"done":{}, "stats":{}}
done=state["done"]
# TorBox reports removed torrents as 500 DATABASE_ERROR rather than 404, so a dead
# release is indistinguishable from a transient blip. Count consecutive failures
# per token and stop after MAX_MATERIALIZE_FAILS runs instead of retrying forever.
fails=state.setdefault("fails",{})
MAX_MATERIALIZE_FAILS=3
BATCH=int(sys.argv[1]) if len(sys.argv)>1 else 40
SLEEP=int(sys.argv[2]) if len(sys.argv)>2 else 90
# 2026-07-28: back-to-back materialize failures are a proxy for a throttled /
# unhealthy TorBox account (the dead-ID 500 storm + CDN 429s). Hard-pause when
# they pile up so the drain stops feeding the throttle even if the playback
# gate somehow misses a viewer -- the ultimate net beneath the gate.
_MATERIALIZE_BACKOFF_AFTER=5
_MATERIALIZE_BACKOFF_S=600
consec_fail=0

# Health gate: don't run while TorBox is unhealthy / rate-limiting.
try:
    if not torbox.get_user_info():
        print("TorBox user_info empty; abort"); sys.exit(0)
except Exception as e:
    print("TorBox health check failed; abort:", e); sys.exit(0)

processed=0
q=open(f"{OUT}/plex_analyze_queue.txt","a")
for rec in TOKENS:
    if processed>=BATCH: break
    tok=rec["token"]
    if tok in done: continue
    while _someone_watching():   # GATE 1: never START an item while anyone streams
        print("playback active -> pausing remediation %ds" % _PLAYBACK_PAUSE_S, flush=True)
        time.sleep(_PLAYBACK_PAUSE_S)
    item=db.get_virtual_item(tok)
    if not item or not item.get("strm_path"):
        done[tok]="no_item"; continue
    try:
        url=catbox.materialize(tok, allow_readd=True)
    except Exception:
        url=None
    if not url:
        state["stats"]["materialize_fail"]=state["stats"].get("materialize_fail",0)+1
        fails[tok]=fails.get(tok,0)+1
        consec_fail+=1
        if fails[tok]>=MAX_MATERIALIZE_FAILS:
            done[tok]="materialize_dead"
            state["stats"]["materialize_dead"]=state["stats"].get("materialize_dead",0)+1
            print(f"giving up on {tok} after {fails[tok]} materialize failures")
        json.dump(state, open(STATE_F,"w"))   # checkpoint so the count survives a kill
        if consec_fail>=_MATERIALIZE_BACKOFF_AFTER:
            print("materialize failing back-to-back (%d) -> TorBox likely throttled; backing off %ds"
                  % (consec_fail, _MATERIALIZE_BACKOFF_S), flush=True)
            time.sleep(_MATERIALIZE_BACKOFF_S)
            consec_fail=0
        processed+=1; time.sleep(SLEEP); continue   # leave undone -> retry next run
    consec_fail=0          # recovered: clear the throttle-backoff counter
    fails.pop(tok, None)   # recovered: clear the strike count
    if _someone_watching():   # GATE 2: viewer arrived mid-item -> skip heavy probe, retry idle
        print("playback started mid-item -> deferring %s" % tok, flush=True)
        continue
    try:
        res=subprocess.run(["ffprobe","-v","quiet","-print_format","json",
                            "-show_streams","-show_format",url],
                           capture_output=True, timeout=60)
        data=json.loads(res.stdout)
    except Exception:
        processed+=1; time.sleep(SLEEP); continue
    streams=data.get("streams",[])
    audio=[s for s in streams if s.get("codec_type")=="audio"]
    subs =[s for s in streams if s.get("codec_type")=="subtitle"]
    video=[s for s in streams if s.get("codec_type")=="video"]
    vcodec=(video[0].get("codec_name") if video else None)
    dur=float(data.get("format",{}).get("duration",0) or 0)
    if not vcodec:
        done[tok]="no_video"; processed+=1; time.sleep(SLEEP); continue
    saved=db.load_spore_tracks(tok) or {}
    v_extra_hex=saved.get("video_extradata_hex") or ""
    if vcodec in ("hevc","h264") and not v_extra_hex:
        # Onyx 2026-07-18: drain used to forward the (empty) saved extradata, so
        # HEVC stubs stayed undecodable on forced transcodes (spore-stream moov
        # had no hvcC). Build the moov cache and pull the real codec-private.
        try:
            _fsh=mp4_faststart._cache_path(tok)
            if os.path.exists(_fsh) and os.path.getsize(_fsh)<1000: os.remove(_fsh)
        except Exception: pass
        try:
            if mp4_faststart.build_and_cache(url, tok):
                _cp=mp4_faststart.extract_codec_private(tok)
                if _cp and len(_cp.hex())>=200 and _cp.hex()[:2]=="01": v_extra_hex=_cp.hex()
        except Exception: pass
    saved.update({"audio":audio,"subs":subs,"duration_s":dur,"video_codec":vcodec,
                  "video_extradata_hex":v_extra_hex})
    db.save_spore_tracks(tok, saved)
    sg.update_stub_from_probe(tok, audio, subs, duration_s=dur or None,
                              video_codec=vcodec,
                              video_extradata_hex=v_extra_hex)
    sp=Path(item["strm_path"]); d=sg._spore_stub_dir(sp)
    mkv=d/(sp.stem+".mkv")
    for f in (mkv, d/(sp.stem+".minfo")):
        if f.exists(): os.chown(f,99,100)
    plex_path=str(mkv).replace("/data/plex-media/series/","/mnt/library/shows/").replace("/data/plex-media/movies/","/mnt/library/movies/")
    q.write(plex_path+"\n"); q.flush()
    done[tok]=vcodec
    state["stats"][vcodec]=state["stats"].get(vcodec,0)+1
    processed+=1
    json.dump(state, open(STATE_F,"w"))   # checkpoint each item (resumable)
    time.sleep(SLEEP)
q.close()
json.dump(state, open(STATE_F,"w"))
remaining=len(TOKENS)-len([t for t in done if done[t] not in ("no_item",)])
print(f"run done: processed={processed} total_done={len(done)}/{len(TOKENS)} stats={state['stats']}")
