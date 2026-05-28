#!/bin/bash
# Mycelium Plex Transcoder wrapper.
# Rewrites -i /plex-media/*.mkv to http://127.0.0.1:8088/spore-stream/<token>
# so FFmpeg reads a moov-first (fast-start) MP4 from the Mycelium proxy,
# bypassing the need for LD_PRELOAD interception in musl-based Plex builds.

newargs=()
found_i=0
spore_replaced=0
spore_minfo=""
for a in "$@"; do
    if [ "$found_i" = "1" ]; then
        found_i=0
        if [[ "$a" == *.mkv ]]; then
            minfo="${a%.mkv}.minfo"
            if [ -f "$minfo" ]; then
                tok=$(grep "^token=" "$minfo" | head -1 | cut -d= -f2)
                if [ -n "$tok" ]; then
                    echo "SPORE-WRAP: -i $a -> http://127.0.0.1:8088/spore-stream/$tok" >&2
                    a="http://127.0.0.1:8088/spore-stream/$tok"
                    spore_replaced=1
                    spore_minfo="$minfo"
                fi
            fi
        fi
    fi
    [ "$a" = "-i" ] && found_i=1
    newargs+=("$a")
done

# When serving CDN content, prevent TrueHD decode errors from blocking the video
# pipeline. Without this, corrupt TrueHD audio causes "Too many packets buffered"
# which crashes the entire mux (video + audio). With delta=0 FFmpeg continues
# muxing video even if the audio pipeline stalls.
if [ "$spore_replaced" = "1" ]; then
    # Remap audio stream when the CDN file has TrueHD as primary audio but a
    # decode-safe fallback (EAC3, AC3, ...) exists at a higher stream index.
    # TrueHD decode fails mid-stream after seeks (missing major-sync), which
    # crashes HLS transcoding on Android/Shield. preferred_audio=N is written
    # by the Mycelium probe logic when TrueHD+fallback is detected.
    preferred_audio=""
    if [ -f "$spore_minfo" ]; then
        preferred_audio=$(grep "^preferred_audio=" "$spore_minfo" | head -1 | cut -d= -f2)
    fi
    if [ -n "$preferred_audio" ] && [ "$preferred_audio" != "0" ]; then
        # Plex routes audio through filter_complex as "[0:N]" where N is the global
        # stream index of the audio track in the input file (stub stream index).
        # The stub always has 1 video track (0:0) so the first audio is at 0:1.
        # In the CDN file the same index 0:1 holds TrueHD; the EAC3 fallback is at
        # 0:(1+preferred_audio). Replace the filter_complex input reference only —
        # do NOT touch -map 0:N or -codec:N args (those are output stream indices).
        stub_audio_idx=1
        cdn_preferred_idx=$((stub_audio_idx + preferred_audio))
        remapped=()
        for arg in "${newargs[@]}"; do
            arg="${arg//\[0:${stub_audio_idx}\]/[0:${cdn_preferred_idx}]}"
            remapped+=("$arg")
        done
        newargs=("${remapped[@]}")
        echo "SPORE-WRAP: remapped filter_complex [0:${stub_audio_idx}] -> [0:${cdn_preferred_idx}] (avoid TrueHD)" >&2
    fi

    # Insert before the last argument (output file or last option).
    # -max_interleave_delta 0   : no delta limit so video keeps flowing if audio stalls
    # -max_muxing_queue_size    : bigger packet buffer so TrueHD seek-sync recovery
    #                             (first few packets after seek may be invalid) doesn't
    #                             overflow before a clean major-sync frame is found.
    last="${newargs[-1]}"
    unset 'newargs[-1]'
    newargs+=("-max_interleave_delta" "0" "-max_muxing_queue_size" "4096" "$last")
    echo "SPORE-WRAP: injected muxer error-tolerance flags" >&2
    echo "SPORE-WRAP: full command: ${newargs[*]}" >&2
fi

exec '/usr/lib/plexmediaserver/Plex Transcoder.real' "${newargs[@]}"
