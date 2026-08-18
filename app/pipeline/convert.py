"""Conversion: M4B sources are passed through untouched (metadata-only
patch happens later, via tag.py); MP3 sources are transcoded to AAC/M4B.
"""
import shutil
from pathlib import Path

from app.config import config
from app.pipeline import ffutil


class ConvertError(RuntimeError):
    pass


def passthrough_m4b(source_file: Path, work_path: Path) -> Path:
    """Copy the M4B into the work dir untouched. No re-encode, no remux."""
    shutil.copy2(source_file, work_path)
    return work_path


def convert_mp3_to_m4b(
    source_files: list[Path],
    work_path: Path,
    log,
    on_progress=None,
    should_cancel=None,
) -> Path:
    bitrates = [ffutil.get_audio_bitrate_kbps(f) for f in source_files]
    max_bitrate = max(bitrates)
    if len(set(bitrates)) > 1:
        log(f"Source files have inconsistent bitrates {bitrates} kbps; using the highest ({max_bitrate}).")

    # Always encode at the source's own bitrate. Re-encoding higher than the
    # source can't recover detail that isn't there - it only wastes space.
    # MIN_BITRATE_KBPS is informational only: a below-floor source is still
    # worth flagging, just not worth "fixing" with a wasteful re-encode.
    if max_bitrate < config.MIN_BITRATE_KBPS:
        log(
            f"Source bitrate ({max_bitrate}kbps) is below the configured "
            f"{config.MIN_BITRATE_KBPS}kbps floor. Encoding at the source's own "
            f"{max_bitrate}kbps anyway - encoding higher can't add back quality "
            "that isn't there."
        )

    total_duration_sec = sum(ffutil.get_duration_sec(f) for f in source_files)

    log(f"Transcoding {len(source_files)} source file(s) to AAC at {max_bitrate}kbps (matching source).")
    ffutil.transcode_to_aac_m4b(
        source_files,
        work_path,
        max_bitrate,
        total_duration_sec=total_duration_sec,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )
    return work_path
