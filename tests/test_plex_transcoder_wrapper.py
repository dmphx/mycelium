import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


WRAPPER = Path(__file__).parents[1] / "spore" / "plex_transcoder_wrapper.sh"


def _force_video_copy(args: list[str]) -> list[str]:
    source = WRAPPER.read_text()
    start_marker = '    if [ "$_do_video_copy" = "1" ]; then'
    end_marker = '    if [ -n "$preferred_audio" ] && [ "$preferred_audio" != "0" ]; then'
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    block = source[start:end]
    words = " ".join(shlex.quote(arg) for arg in args)
    script = f"""
SPORE_LOG=/dev/null
_vcodec_post=h264
_do_video_copy=1
newargs=({words})
{block}
printf '%s\\0' "${{newargs[@]}}"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        capture_output=True,
    )
    return result.stdout.rstrip(b"\0").decode().split("\0")


def _run_wrapper(tmp_path: Path, args: list[str], audio_codec: str | None) -> list[str]:
    fake_transcoder = tmp_path / "fake-transcoder"
    fake_transcoder.write_text("#!/bin/bash\nprintf '%s\\0' \"$@\"\n")
    fake_transcoder.chmod(0o755)

    stub = tmp_path / "movie.mkv"
    stub.touch()
    if audio_codec is not None:
        stub.with_suffix(".minfo").write_text(
            f"token=12345678abcdef00\ncdn_audio_codec={audio_codec}\n"
        )

    source = WRAPPER.read_text()
    source = source.replace(
        "SPORE_LOG=/config/spore-wrap-debug.log",
        "SPORE_LOG=/dev/null",
    ).replace(
        "FFMPEG_STDERR_LOG=/config/spore-ffmpeg-stderr.log",
        "FFMPEG_STDERR_LOG=/dev/null",
    ).replace(
        "'/usr/lib/plexmediaserver/Plex Transcoder.real'",
        shlex.quote(str(fake_transcoder)),
    ).replace(
        'last="${newargs[-1]}"\n    unset \'newargs[-1]\'',
        'last_idx=$((${#newargs[@]} - 1))\n'
        '    last="${newargs[$last_idx]}"\n'
        "    unset 'newargs[$last_idx]'",
    )

    result = subprocess.run(
        ["bash", "-c", source, "wrapper-test", *args[: args.index("-i") + 1], str(stub), *args[args.index("-i") + 2 :]],
        check=True,
        capture_output=True,
    )
    return result.stdout.rstrip(b"\0").decode().split("\0")


@pytest.mark.parametrize(
    ("video_graph", "video_map", "audio_graph", "audio_map"),
    [
        (
            "[0:2]scale=1920:1080[0];[0:0]scale=w=1920:h=1080[1];[1][0]overlay[3]",
            "[3]",
            "[0:1] aresample=async=1:ochl='stereo'[4]",
            "[4]",
        ),
        (
            "[0:2]scale=1920:1080[0];[0:#0x01]scale=w=1920:h=1080[1];"
            "[1][0]overlay[3];[3]hwupload[4]",
            "[4]",
            "[0:1] aresample=async=1:ochl='stereo'[5]",
            "[5]",
        ),
    ],
)
def test_subtitle_first_video_filter_is_removed(
    video_graph: str,
    video_map: str,
    audio_graph: str,
    audio_map: str,
) -> None:
    transformed = _force_video_copy(
        [
            "-i",
            "http://mycelium/spore-stream/test",
            "-filter_complex",
            video_graph,
            "-map",
            video_map,
            "-codec:0",
            "copy",
            "-filter_complex",
            audio_graph,
            "-map",
            audio_map,
            "-codec:1",
            "aac",
            "dash",
        ]
    )

    assert video_graph not in transformed
    assert video_map not in transformed
    assert transformed.count("-filter_complex") == 1
    assert audio_graph in transformed
    assert audio_map in transformed
    assert transformed[transformed.index("-map") + 1] == "0:0"


def test_native_aac_replaces_stale_eae_decoder_hint(tmp_path: Path) -> None:
    video_graph = (
        "[0:2]scale=1920:1080[0];[0:0]scale=w=1920:h=1080[1];"
        "[1][0]overlay[3];[3]hwupload[4]"
    )
    audio_graph = "[0:1] aresample=async=1:ochl='stereo'[5]"
    transformed = _run_wrapper(
        tmp_path,
        [
            "-codec:0",
            "h264",
            "-codec:1",
            "eac3_eae",
            "-eae_prefix:1",
            "fixture_",
            "-i",
            "ignored-by-helper",
            "-filter_complex",
            video_graph,
            "-map",
            "[4]",
            "-codec:0",
            "h264_vaapi",
            "-filter_complex",
            audio_graph,
            "-map",
            "[5]",
            "-codec:1",
            "aac",
            "dash",
        ],
        audio_codec="aac",
    )

    input_index = transformed.index("-i")
    assert transformed[input_index - 2 : input_index] == ["-codec:1", "aac"]
    assert "eac3_eae" not in transformed
    assert "-eae_prefix:1" not in transformed
    assert video_graph not in transformed
    assert audio_graph in transformed
    assert "[5]" in transformed


def _map_values(args: list[str]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-map"]


@pytest.mark.parametrize("output", ["chunk-%05d", "media-%05d.ts", "output.mkv"])
@pytest.mark.parametrize(
    ("video", "audio", "subtitle"),
    [("0:0", "0:1", "0:2"), ("0:#0x01", "0:#0x02", "0:#0x03"),
     ("0:0", "0:3", "0:2"), ("0:#0x0100", "0:#0x0110", "0:#0x0120")],
)
def test_missing_subtitle_map_optional_for_every_output(tmp_path, output, video, audio, subtitle):
    # Plex DASH uses ONE segment/matroska output, with all three streams mapped
    # before chunk-%05d. There is no preceding HLS media-%05d output.
    args = [
        "-codec:0", "h264", "-codec:1", "eac3_eae", "-i", "fixture.mkv",
        "-map", video, "-metadata:s:0", "language=eng", "-codec:0", "copy",
        "-map", audio, "-metadata:s:1", "language=eng", "-codec:1", "copy",
        "-map", subtitle, "-metadata:s:2", "language=eng", "-codec:2", "copy",
        "-strict_ts:2", "0", "-f", "segment", "-segment_format", "matroska",
        "-segment_time", "4", "-segment_list", "segments.csv", output,
    ]
    transformed = _run_wrapper(tmp_path, args, audio_codec="aac")
    assert _map_values(transformed) == [video, audio, subtitle + "?"]
    assert transformed[-1] == output
    assert ["-strict_ts:2", "0"] == transformed[
        transformed.index("-strict_ts:2"):transformed.index("-strict_ts:2") + 2
    ]


def test_optional_maps_do_not_change_labels_other_inputs_or_negative_maps(tmp_path):
    args = ["-i", "fixture.mkv", "-map", "0:0", "-codec:0", "copy",
            "-map", "[audio]", "-codec:1", "aac", "-map", "0:2?",
            "-map", "1:2", "-map", "-0:3", "-map", "0:s:0", "chunk-%05d"]
    transformed = _run_wrapper(tmp_path, args, audio_codec="aac")
    assert _map_values(transformed) == ["0:0", "[audio]", "0:2?", "1:2", "-0:3", "0:s:0"]


def test_hls_required_streams_stay_required_in_later_outputs(tmp_path):
    args = ["-i", "fixture.mkv", "-map", "0:#0x01", "-codec:0", "copy",
            "-map", "0:#0x02", "-codec:1", "copy", "media-%05d.ts",
            "-map", "0:#0x0001", "-map", "0:#0X0002", "-map", "0:#0x03", "subtitle-%05d.vtt"]
    transformed = _run_wrapper(tmp_path, args, audio_codec="aac")
    assert _map_values(transformed) == ["0:#0x01", "0:#0x02", "0:#0x0001", "0:#0X0002", "0:#0x03?"]


def test_hex_video_decoder_keeps_required_hevc_transcode(tmp_path):
    args = ["-codec:#0x01", "hevc", "-i", "fixture.mkv",
            "-map", "0:#0x01", "-codec:0", "h264", "-map", "0:#0x02", "-codec:1", "aac", "chunk-%05d"]
    transformed = _run_wrapper(tmp_path, args, audio_codec="aac")
    assert transformed[transformed.index("-codec:0") + 1] == "h264"


@pytest.mark.parametrize("case", json.loads(
    (Path(__file__).parent / "fixtures" / "plex-dash-chunk-argv.json").read_text()
)["cases"], ids=lambda case: case["name"])
def test_recorded_plex_dash_argv(tmp_path, case):
    transformed = _run_wrapper(tmp_path, case["argv"], audio_codec="aac")
    expected = ["0:0", "[3]"]
    if case["has_map_0_2"]:
        expected += ["0:2?", "0:t?"]
    assert _map_values(transformed) == expected
    assert transformed[-1] == "chunk-%05d"
    assert "-codec:2" not in transformed or transformed[transformed.index("-codec:2") + 1] == "copy"


@pytest.mark.parametrize("existing", ["", "/caller/driver"])
def test_vaapi_driver_discovery_preserves_existing_override(tmp_path, existing):
    source = WRAPPER.read_text()
    start = source.index("# ── Ensure VAAPI can find")
    end = source.index('\nif [ "$spore_replaced" = "1" ]; then', start)
    driver = tmp_path / "driver"
    driver.mkdir()
    (driver / "iHD_drv_video.so").touch()
    block = source[start:end].replace(
        "/config/Library/Application Support/Plex Media Server/Cache/va-dri-linux-x86_64", str(driver)
    )
    script = (f"SPORE_LOG=/dev/null\nLIBVA_DRIVERS_PATH={shlex.quote(existing)}\n"
              "newargs=(-codec:0 h264_vaapi)\n" + block + '\nprintf "%s" "$LIBVA_DRIVERS_PATH"\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, check=True, text=True)
    assert result.stdout == (existing or str(driver))


def test_missing_auxiliary_stream_allows_real_segment_muxing(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the bounded local muxer check")
    movie = tmp_path / "real.mkv"
    generate = subprocess.run([
        ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", "0.5",
        "-c:v", "ffv1", "-threads", "1", "-c:a", "pcm_s16le", str(movie)
    ], capture_output=True, timeout=15)
    assert generate.returncode == 0, generate.stderr.decode()
    for missing, expected_success in [(2, True), (9, False)]:
        args = ["-i", "fixture.mkv", "-map", "0:0", "-codec:0", "copy",
                "-map", "0:1" if missing == 2 else "0:9", "-codec:1", "copy",
                "-map", "0:2", "-codec:2", "copy", "chunk-%05d"]
        maps = _map_values(_run_wrapper(tmp_path, args, audio_codec="aac"))
        # Execute only the transformed maps with stock FFmpeg. Plex-specific
        # options/manifest callbacks require a separate real PMS session check.
        command = [ffmpeg, "-nostdin", "-v", "error", "-i", str(movie)]
        for value in maps:
            command += ["-map", value]
        result = subprocess.run(command + ["-c", "copy", "-f", "segment", "-segment_format", "matroska",
                                           str(tmp_path / f"case-{missing}-%05d.mkv")],
                                capture_output=True, timeout=15)
        assert (result.returncode == 0) == expected_success, result.stderr.decode()
        if expected_success:
            assert list(tmp_path.glob(f"case-{missing}-*.mkv"))
        else:
            assert b"matches no streams" in result.stderr


def test_non_spore_maps_are_untouched(tmp_path):
    args = ["-i", "fixture.mkv", "-map", "0:0", "-codec:0", "copy",
            "-map", "0:1", "-codec:1", "copy", "-map", "0:2", "chunk-%05d"]
    assert _map_values(_run_wrapper(tmp_path, args, audio_codec=None)) == ["0:0", "0:1", "0:2"]
