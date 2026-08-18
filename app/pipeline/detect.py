"""Format detection: figure out which of the three supported input shapes
a dropped-off item is, and the ordered list of audio files it contains.
"""
import re
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".m4b"}
IGNORED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".txt", ".nfo", ".cue", ".log", ".ds_store"}


class DetectionError(ValueError):
    pass


@dataclass
class DetectionResult:
    source_type: str  # m4b_single | mp3_multi | mp3_single
    audio_files: list  # ordered list[Path]
    title_guess: str
    author_guess: str
    ignored_files: list


def _natural_sort_key(path: Path):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def _guess_title_author(name: str) -> tuple[str, str]:
    """Best-effort split of a filename/foldername into (title, author).

    Handles the common "Author - Title" convention; otherwise treats the
    whole name as the title with no author guess.
    """
    cleaned = re.sub(r"[._]+", " ", name).strip()
    if " - " in cleaned:
        left, right = cleaned.split(" - ", 1)
        return right.strip(), left.strip()
    return cleaned, ""


def detect(source_path: Path) -> DetectionResult:
    if source_path.is_file():
        suffix = source_path.suffix.lower()
        if suffix == ".m4b":
            title, author = _guess_title_author(source_path.stem)
            return DetectionResult("m4b_single", [source_path], title, author, [])
        if suffix == ".mp3":
            title, author = _guess_title_author(source_path.stem)
            return DetectionResult("mp3_single", [source_path], title, author, [])
        raise DetectionError(
            f"Unsupported file type '{suffix}' for {source_path.name}. "
            "Expected a .m4b or .mp3 file, or a folder of .mp3 files."
        )

    if source_path.is_dir():
        all_files = [p for p in sorted(source_path.iterdir()) if p.is_file()]
        audio_files = [p for p in all_files if p.suffix.lower() in AUDIO_EXTENSIONS]
        ignored = [
            p for p in all_files
            if p.suffix.lower() not in AUDIO_EXTENSIONS and not p.name.startswith(".")
        ]

        if not audio_files:
            raise DetectionError(f"No .mp3 or .m4b files found in {source_path.name}.")

        extensions_present = {p.suffix.lower() for p in audio_files}
        if len(extensions_present) > 1:
            raise DetectionError(
                f"{source_path.name} mixes .mp3 and .m4b files ({sorted(extensions_present)}). "
                "A single audiobook source should be entirely one or the other."
            )

        title, author = _guess_title_author(source_path.name)
        ext = extensions_present.pop()

        if ext == ".m4b":
            if len(audio_files) > 1:
                raise DetectionError(
                    f"{source_path.name} contains {len(audio_files)} .m4b files; "
                    "expected exactly one."
                )
            return DetectionResult("m4b_single", audio_files, title, author, ignored)

        # .mp3
        sorted_files = sorted(audio_files, key=_natural_sort_key)
        source_type = "mp3_multi" if len(sorted_files) > 1 else "mp3_single"
        return DetectionResult(source_type, sorted_files, title, author, ignored)

    raise DetectionError(f"{source_path} is neither a file nor a directory.")
