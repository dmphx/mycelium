import shlex
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


def _run_wrapper(tmp_path: Path, args: list[str], audio_codec: str) -> list[str]:
    fake_transcoder = tmp_path / "fake-transcoder"
    fake_transcoder.write_text("#!/bin/bash\nprintf '%s\\0' \"$@\"\n")
    fake_transcoder.chmod(0o755)

    stub = tmp_path / "movie.mkv"
    stub.touch()
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
