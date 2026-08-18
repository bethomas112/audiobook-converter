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


def convert_mp3_to_m4b(source_files: list[Path], work_path: Path, log) -> Path:
    bitrates = [ffutil.get_audio_bitrate_kbps(f) for f in source_files]
    max_bitrate = max(bitrates)
    if len(set(bitrates)) > 1:
        log(f"Source files have inconsistent bitrates {bitrates} kbps; using the highest ({max_bitrate}).")

    target_bitrate = max(max_bitrate, config.MIN_BITRATE_KBPS)
    if target_bitrate > max_bitrate:
        log(f"Source bitrate ({max_bitrate}kbps) is below the configured floor; encoding at {target_bitrate}kbps.")

    log(f"Transcoding {len(source_files)} source file(s) to AAC at {target_bitrate}kbps.")
    ffutil.transcode_to_aac_m4b(source_files, work_path, target_bitrate)
    return work_path
