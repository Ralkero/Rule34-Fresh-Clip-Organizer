#!/usr/bin/env python3
"""
Rule34 Fresh Clip Organizer.

Two-step workflow:
  preview --source SOURCE_DIR
  apply --plan PREVIEW_CSV

The preview command writes an editable CSV plan and a Markdown summary. The
apply command only moves rows approved in that plan and never overwrites.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


VERSION = "0.1.1"

CSV_COLUMNS = [
    "approved",
    "source_path",
    "original_name",
    "artist",
    "character",
    "character_confidence",
    "character_reason",
    "clean_title",
    "resolution",
    "target_folder",
    "target_filename",
    "target_path",
    "confidence",
    "artist_confidence",
    "character_confidence_component",
    "franchise_confidence",
    "title_confidence",
    "resolution_confidence",
    "weighted_confidence",
    "status",
    "reason",
    "notes",
]

OPTIONAL_CSV_COLUMNS = {"character", "character_confidence", "character_reason", "artist_confidence", "character_confidence_component", "franchise_confidence", "title_confidence", "resolution_confidence", "weighted_confidence"}
REQUIRED_CSV_COLUMNS = [col for col in CSV_COLUMNS if col not in OPTIONAL_CSV_COLUMNS]

BLOCKED_STATUSES = {"blocked", "duplicate", "missing_source", "unmatched", "invalid"}
APPROVED_TRUE = {"1", "true", "yes", "y", "approved", "apply"}
APPROVED_FALSE = {"", "0", "false", "no", "n", "skip"}
DATE_PREFIX_RE = re.compile(r"^\s*(?:19|20)\d{2}[-_. ]\d{1,2}[-_. ]\d{1,2}(?:\s*[-_. ]\s*|\s+)(.+)$")

# Matches common Rule34 collector uploads: "ArtistYYMMDD Title..." or "Artist YYYYMMDD Title..."
# e.g. "Mai 210704 Nude multiaudio.mp4", "Sinia 210422 multiaudio.mp4"
COMPACT_DATE_ARTIST_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]{1,11})\s+(\d{6,8})\s+(.+)$")

PRECEDENT_STOP_TOKENS = {
    "a",
    "an",
    "and",
    "animated",
    "anniversary",
    "alt",
    "bath",
    "bonus",
    "clip",
    "concept",
    "day",
    "extra",
    "full",
    "girl",
    "hot",
    "loop",
    "melon",
    "new",
    "night",
    "part",
    "scene",
    "the",
    "valentine",
    "wife",
}

GENERIC_SOURCE_FOLDER_NAMES = {
    "animation",
    "animations",
    "clip",
    "clips",
    "download",
    "downloads",
    "fresh",
    "incoming",
    "new",
    "new clips",
    "to sort",
    "unsorted",
    "video",
    "videos",
}

SOURCE_ARTIST_SUFFIX_RE = re.compile(
    r"(?i)\s+(?:artist\s+)?(?:collection|clips?|videos?|animations?)\s*$"
)

DEFAULT_RESOLUTION_LABELS = {
    "8k": "8K",
    "4k": "4K",
    "1440": "4K",
    "1080": "1080P",
    "720": "720P",
    "480": "480P",
}

RESOLUTION_BUCKET_ORDER = ("8k", "4k", "1440", "1080", "720", "480")


@dataclass(frozen=True)
class Config:
    destination_root: Path
    video_extensions: Tuple[str, ...]
    ffprobe_path: str
    review_folder_name: str
    content_review_folder_name: str
    confidence_threshold: float
    allow_create_destination_folders: bool
    artist_aliases: Dict[str, str]
    folder_aliases: Dict[str, str]
    character_mappings: Dict[str, str]
    canonical_character_aliases: Dict[str, str]
    title_token_replacements: Dict[str, str]
    content_review_terms: Dict[str, Tuple[str, ...]]
    junk_tokens: Tuple[str, ...]
    preserve_tokens: Tuple[str, ...]
    audio_credits: Tuple[str, ...]
    known_collectors: Tuple[str, ...]
    collection_folder_indicators: Tuple[str, ...]
    # Optional AI assistance for unknown characters/franchises (uses xAI Grok API)
    use_ai_for_unknown_characters: bool
    ai_model: str
    ai_api_key_env_var: str
    # New production hardening options
    original_character_subfoldering: bool
    learned_franchises_file: str
    extract_embedded_titles: bool

@dataclass
class NamingStyle:
    sample_count: int
    resolution_labels: Dict[str, str]
    learned_resolution_buckets: Tuple[str, ...]


@dataclass
class ReferenceData:
    destination_folders: Dict[str, str]
    artist_precedent: Dict[str, str]
    token_precedent: Dict[str, Dict[str, int]]
    canonical_character_aliases: Dict[str, str]
    naming_style: NamingStyle
    learned_franchises: Dict[str, str] = None  # character_norm -> franchise from confirmed Grok suggestions


@dataclass(frozen=True)
class CharacterDetection:
    characters: Tuple[str, ...]
    confidence: float
    reason: str
    matched_aliases: Tuple[str, ...]


@dataclass(frozen=True)
class RowConfidence:
    """Structured confidence components to replace simple min() collapse.

    Stores individual confidences so CSV can expose transparency, and
    weighted final score can be computed. All values in [0.0, 1.0].
    """
    artist: float = 0.0
    character: float = 0.0
    franchise: float = 0.0  # folder / target_folder
    title: float = 0.0
    resolution: float = 0.0

    def final(self, weights: Optional[Dict[str, float]] = None) -> float:
        """Compute weighted average. Default weights favor artist + franchise."""
        if weights is None:
            weights = {"artist": 0.30, "character": 0.15, "franchise": 0.30, "title": 0.10, "resolution": 0.15}
        total = 0.0
        wsum = 0.0
        for k, w in weights.items():
            val = getattr(self, k, 0.0)
            total += val * w
            wsum += w
        return total / wsum if wsum > 0 else 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "artist_confidence": self.artist,
            "character_confidence": self.character,
            "franchise_confidence": self.franchise,
            "title_confidence": self.title,
            "resolution_confidence": self.resolution,
            "confidence": self.final(),
        }


def normalize(text: object) -> str:
    if text is None:
        return ""
    value = str(text).lower()
    value = value.replace("&", " and ")
    value = value.replace("'", "")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def title_case_words(text: str, preserve_tokens: Sequence[str]) -> str:
    preserve = {normalize(token): token for token in preserve_tokens}
    words = re.split(r"(\s+)", text.strip())
    out: List[str] = []
    for word in words:
        if not word or word.isspace():
            out.append(word)
            continue
        key = normalize(word)
        if key in preserve:
            out.append(preserve[key])
        elif re.fullmatch(r"[A-Z0-9.]+", word) and any(ch.isdigit() for ch in word):
            out.append(word)
        elif "-" in word:
            out.append("-".join(part.capitalize() if part else part for part in word.split("-")))
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return "".join(out).strip()


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    preserve_tokens = tuple(raw.get("preserve_tokens", []))
    character_mappings = {normalize(k): v for k, v in raw.get("character_mappings", {}).items()}
    canonical_character_aliases = {
        normalize(k): v for k, v in raw.get("canonical_character_aliases", {}).items()
    }
    for alias_norm in character_mappings:
        canonical_character_aliases.setdefault(alias_norm, title_case_words(alias_norm, preserve_tokens))

    return Config(
        destination_root=Path(raw["destination_root"]),
        video_extensions=tuple(ext.lower() for ext in raw.get("video_extensions", [".mp4"])),
        ffprobe_path=str(raw.get("ffprobe_path", "ffprobe")),
        review_folder_name=str(raw.get("review_folder_name", "_r34_review")),
        content_review_folder_name=str(raw.get("content_review_folder_name", "_r34_content_review")),
        confidence_threshold=float(raw.get("confidence_threshold", 0.9)),
        allow_create_destination_folders=bool(raw.get("allow_create_destination_folders", False)),
        artist_aliases={normalize(k): v for k, v in raw.get("artist_aliases", {}).items()},
        folder_aliases={normalize(k): v for k, v in raw.get("folder_aliases", {}).items()},
        character_mappings=character_mappings,
        canonical_character_aliases=canonical_character_aliases,
        title_token_replacements={normalize(k): v for k, v in raw.get("title_token_replacements", {}).items()},
        content_review_terms={
            str(category): tuple(str(term) for term in terms)
            for category, terms in raw.get("content_review_terms", {}).items()
        },
        junk_tokens=tuple(raw.get("junk_tokens", [])),
        preserve_tokens=preserve_tokens,
        audio_credits=tuple(str(x) for x in raw.get("audio_credits", [])),
        known_collectors=tuple(str(x) for x in raw.get("known_collectors", [])),
        collection_folder_indicators=tuple(str(x).lower() for x in raw.get("collection_folder_indicators", [])),
        use_ai_for_unknown_characters=bool(raw.get("use_ai_for_unknown_characters", False)),
        ai_model=str(raw.get("ai_model", "grok-3")),
        ai_api_key_env_var=str(raw.get("ai_api_key_env_var", "XAI_API_KEY")),
        original_character_subfoldering=bool(raw.get("original_character_subfoldering", False)),
        learned_franchises_file=str(raw.get("learned_franchises_file", "learned_character_franchises.json")),
        extract_embedded_titles=bool(raw.get("extract_embedded_titles", False)),
    )


def default_config_path() -> Path:
    return Path(__file__).with_name("r34_config.json")


def load_learned_franchises(config: Config) -> Dict[str, str]:
    """Load manually confirmed learned franchises (safe, user-reviewed)."""
    p = Path(config.learned_franchises_file)
    if not p.exists():
        # also check next to config
        p = default_config_path().with_name(p.name)
    if p.exists():
        try:
            return {normalize(k): v for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
        except Exception:
            return {}
    return {}


def write_pending_learned_franchises(new_mappings: Dict[str, str], config: Config) -> Path:
    """Write new Grok-derived mappings to .pending.json for manual review/rename."""
    if not new_mappings:
        return Path()
    p = Path(config.learned_franchises_file).with_suffix(".pending.json")
    if not p.parent.exists():
        p = default_config_path().with_name(p.name)
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.update({k: v for k, v in new_mappings.items() if k not in existing})
    p.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def is_under_review_dir(path: Path, config: Config) -> bool:
    review_folders = {config.review_folder_name, config.content_review_folder_name}
    return any(part in review_folders for part in path.parts)


def discover_videos(source: Path, config: Config) -> List[Path]:
    files: List[Path] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if is_under_review_dir(path, config):
            continue
        if path.suffix.lower() in config.video_extensions:
            files.append(path)
    return sorted(files, key=lambda p: str(p).lower())


def parse_existing_filename(name: str) -> Optional[Tuple[str, str, str]]:
    match = re.match(r"^(.+?) - (.+?) \[([^\]]+)\]\.[^.]+$", name)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()


def resolution_label_bucket(label: str) -> str:
    value = str(label or "").strip().lower()
    value = re.sub(r"\s+", "", value)
    value = value.replace("uhd", "2160p")
    if value in {"8k", "4320", "4320p"}:
        return "8k"
    if value in {"4k", "2160", "2160p"}:
        return "4k"
    match = re.match(r"^(1440|1080|720|480)p?$", value)
    if match:
        return match.group(1)
    if re.match(r"^\d{2,5}x\d{2,5}$", value):
        return value
    return value


def sorted_resolution_buckets(buckets: Iterable[str]) -> Tuple[str, ...]:
    def sort_key(bucket: str) -> Tuple[int, str]:
        if bucket in RESOLUTION_BUCKET_ORDER:
            return RESOLUTION_BUCKET_ORDER.index(bucket), bucket
        return len(RESOLUTION_BUCKET_ORDER), bucket

    return tuple(sorted(buckets, key=sort_key))


def output_resolution_label(bucket: str, observed_label: str = "") -> str:
    if bucket in DEFAULT_RESOLUTION_LABELS:
        return DEFAULT_RESOLUTION_LABELS[bucket]
    if observed_label:
        return observed_label.upper()
    return bucket.upper()


def build_reference_data(destination_root: Path, config: Optional[Config] = None) -> ReferenceData:
    folders: Dict[str, str] = {}
    artist_precedent: Dict[str, str] = {}
    token_precedent: Dict[str, Dict[str, int]] = {}
    canonical_character_aliases: Dict[str, str] = {}
    resolution_counts: Dict[str, Counter] = {}
    naming_sample_count = 0
    if config:
        canonical_character_aliases.update(config.canonical_character_aliases)

    if destination_root.exists():
        for child in destination_root.iterdir():
            if child.is_dir() and not child.name.startswith("_"):
                folders[normalize(child.name)] = child.name

        for file_path in destination_root.rglob("*"):
            if not file_path.is_file():
                continue
            parsed = parse_existing_filename(file_path.name)
            if not parsed:
                continue
            artist, title, resolution = parsed
            naming_sample_count += 1
            resolution_bucket = resolution_label_bucket(resolution)
            if resolution_bucket:
                resolution_counts.setdefault(resolution_bucket, Counter())
                resolution_counts[resolution_bucket][resolution] += 1
            learned_character = likely_canonical_character_from_title(title)
            if learned_character:
                add_canonical_character_aliases(canonical_character_aliases, learned_character)
            folder = file_path.parent.name
            artist_precedent.setdefault(normalize(artist), artist)
            for token in title_tokens(title):
                if len(token) < 2:
                    continue
                token_precedent.setdefault(token, {})
                token_precedent[token][folder] = token_precedent[token].get(folder, 0) + 1

    resolution_labels = dict(DEFAULT_RESOLUTION_LABELS)
    for bucket, counts in resolution_counts.items():
        resolution_labels[bucket] = output_resolution_label(bucket, counts.most_common(1)[0][0])

    naming_style = NamingStyle(
        sample_count=naming_sample_count,
        resolution_labels=resolution_labels,
        learned_resolution_buckets=sorted_resolution_buckets(resolution_counts.keys()),
    )
    learned = load_learned_franchises(config) if config else {}
    if learned:
        for ck, folder in learned.items():
            if ck not in character_mappings:
                pass
    ref_learned = learned or {}
    return ReferenceData(folders, artist_precedent, token_precedent, canonical_character_aliases, naming_style, learned_franchises=ref_learned)


def likely_canonical_character_from_title(title: str) -> str:
    if " - " not in title:
        return ""
    candidate = title.split(" - ", 1)[0].strip()
    normalized = normalize(candidate)
    if not normalized:
        return ""
    words = normalized.split()
    if len(words) > 7:
        return ""
    if normalized in PRECEDENT_STOP_TOKENS:
        return ""
    if any(token in PRECEDENT_STOP_TOKENS for token in words) and len(words) <= 2:
        return ""
    return candidate


def add_canonical_character_aliases(alias_map: Dict[str, str], character: str) -> None:
    for part in character_parts(character):
        for alias in character_alias_candidates(part):
            alias_norm = normalize(alias)
            if alias_norm:
                alias_map.setdefault(alias_norm, part)


def character_parts(character: str) -> List[str]:
    parts = re.split(r"\s*,\s*|\s+&\s+", character)
    return [part.strip() for part in parts if part.strip()]


def character_alias_candidates(character: str) -> List[str]:
    aliases = [character]
    without_paren = re.sub(r"\s*\(.+?\)", "", character).strip()
    if without_paren:
        aliases.append(without_paren)
        first = without_paren.split()[0]
        first_norm = normalize(first)
        if (
            len(first_norm) >= 4
            and first_norm not in {"princess", "queen", "lady"}
        ):
            aliases.append(first)
    for paren in re.findall(r"\((.+?)\)", character):
        if paren.strip():
            aliases.append(paren.strip())
    return aliases


def infer_unmatched_character(text: str, reference: ReferenceData, config: Config) -> str:
    """Fallback extractor for characters the current reference does not know.

    When detect_characters returns no match, we still want to surface a plausible
    character name from the title (use the name "as it would any other") and
    add it to the live ReferenceData.canonical_character_aliases so the rest of
    the preview run (and title stripping for folder classification) benefits.
    This is the "learning new characters into the database being built" behavior.
    """
    if not text:
        return ""

    value = text
    value = re.sub(r'[\[\]\(\)\{\}_]', ' ', value)
    value = remove_resolution_text(value)

    # Aggressively remove known audio credits and junk so we don't promote them as characters
    for credit in getattr(config, "audio_credits", ()):
        value = re.sub(r"\b" + re.escape(credit) + r"\b", " ", value, flags=re.I)
    for junk in getattr(config, "junk_tokens", ()):
        value = re.sub(r"\b" + re.escape(junk) + r"\b", " ", value, flags=re.I)

    # Tokenize - allow digits inside names (NewGirl69, etc.)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9']*", value)
    if not tokens:
        return ""

    # Collect leading name-like tokens. Stop at clear action/stop words.
    stop_words = set(PRECEDENT_STOP_TOKENS) | {
        "with", "and", "getting", "fucked", "pounded", "riding", "sucking",
        "fucking", "creampie", "anal", "pov", "bj", "from", "by", "in", "on",
        "hard", "deep", "fast", "slow"
    }
    name_tokens: List[str] = []
    for t in tokens:
        nt = normalize(t)
        if nt in stop_words or len(nt) < 2:
            if name_tokens:
                break
            continue
        name_tokens.append(t)
        if len(name_tokens) >= 5:
            break

    if not name_tokens:
        return ""

    candidate = " ".join(name_tokens).strip()
    if len(candidate) < 2 or len(candidate) > 60:
        return ""

    return title_case_words(candidate, config.preserve_tokens)


def title_tokens(text: str) -> List[str]:
    normalized = normalize(text)
    tokens = normalized.split()
    phrases = set(tokens)
    for size in (2, 3):
        for idx in range(0, max(0, len(tokens) - size + 1)):
            phrases.add(" ".join(tokens[idx : idx + size]))
    return sorted(phrases, key=lambda t: (-len(t.split()), -len(t), t))


def strip_leading_index(stem: str) -> str:
    value = stem.strip()
    if DATE_PREFIX_RE.match(value):
        return value
    value = re.sub(r"^\s*\(\s*\d+\s*\)\s*", "", value)
    value = re.sub(r"^\s*\d+\s*[-_. ]+\s*", "", value)
    return value.strip()


def is_generic_source_folder(name: str) -> bool:
    return normalize(name) in GENERIC_SOURCE_FOLDER_NAMES


def strip_source_artist_suffix(name: str) -> str:
    value = str(name or "").strip(" -_.")
    stripped = SOURCE_ARTIST_SUFFIX_RE.sub("", value).strip(" -_.")
    if stripped and normalize(stripped) != normalize(value):
        return stripped
    return ""


def source_artist_display_name(raw_name: str, config: Config) -> str:
    value = re.sub(r"\s+", " ", str(raw_name or "")).strip(" -_.")
    if not value:
        return ""
    # Preserve artist handles such as xHoly3Dx or SageOfOsiris instead of
    # flattening their intentional internal capitalization.
    if re.search(r"[a-z][A-Z]", value):
        return value
    return title_case_words(value, config.preserve_tokens)


def known_artist_from_text(raw_artist: str, config: Config, reference: ReferenceData) -> str:
    norm = normalize(raw_artist)
    if norm in config.artist_aliases:
        return config.artist_aliases[norm]
    if norm in reference.artist_precedent:
        return reference.artist_precedent[norm]
    compact = norm.replace(" ", "")
    if compact in config.artist_aliases:
        return config.artist_aliases[compact]
    return ""


def is_known_artist_text(raw_artist: str, config: Config, reference: ReferenceData) -> bool:
    return bool(known_artist_from_text(raw_artist, config, reference))


def looks_like_artist_prefix(token: str, config: Config, reference: ReferenceData, is_collector_source: bool = False) -> bool:
    """Heuristic: is this token more likely an artist name than a character or junk.

    When is_collector_source=True, we do NOT penalize the token for also being a known character name,
    because in collector folders the actual artist is frequently a short handle that matches a character.
    """
    if not token or len(token) < 2:
        return False
    norm = normalize(token)
    if is_known_artist_text(token, config, reference):
        return True

    # Only penalize strong character matches when we are NOT inside a collector folder.
    if not is_collector_source:
        char_det = detect_characters(token, reference)
        if char_det.characters and char_det.confidence > 0.85:
            return False

    if norm in PRECEDENT_STOP_TOKENS:
        return False

    # Short, non-junk tokens are plausible artist handles.
    # Collector sources frequently use very short handles (Mai, Sinia, etc.).
    min_len = 2 if is_collector_source else 3
    return len(norm) >= min_len


def source_context_artist(source: Path, config: Config, reference: ReferenceData) -> Tuple[str, float, str]:
    candidates = [source.name, source.parent.name]

    # De-prioritize known collector/uploader folder names and collection-style folders
    known_collectors = getattr(config, "known_collectors", ())
    collection_indicators = getattr(config, "collection_folder_indicators", ())
    normalized_collectors = {normalize(c) for c in known_collectors}
    normalized_indicators = {normalize(i) for i in collection_indicators}

    def is_collector_folder(name: str) -> bool:
        n = normalize(name)
        return n in normalized_collectors or any(ind in n for ind in normalized_indicators) or is_generic_source_folder(name)

    for candidate in candidates:
        norm = normalize(candidate)
        if norm in normalized_collectors:
            continue
        if norm in config.artist_aliases:
            return config.artist_aliases[norm], 0.96, "artist_from_source_alias"
        if norm in reference.artist_precedent:
            return reference.artist_precedent[norm], 0.82, "artist_from_source_precedent"

    for candidate in candidates:
        # For collection/uploader folders, we still want the *base artist name* (e.g. "Lazy Procrastinator Collection" → "Lazy Procrastinator").
        # Only fully skip pure known collectors or folders that have no usable artist part after stripping.
        if normalize(candidate) in normalized_collectors:
            continue
        stripped = strip_source_artist_suffix(candidate)
        if stripped and not is_generic_source_folder(stripped):
            known = known_artist_from_text(stripped, config, reference)
            if known:
                return known, 0.96, "artist_from_source_collection_alias"
            return source_artist_display_name(stripped, config), 0.92, "artist_from_source_collection"
        # If no good stripped form and it is a pure collector/generic, skip to fallback
        if is_collector_folder(candidate):
            continue
        # Non-collector with no suffix: fall through to precedent checks below if any
        if candidate.strip():
            known = known_artist_from_text(candidate, config, reference)
            if known:
                return known, 0.96, "artist_from_source_collection_alias"
            return source_artist_display_name(candidate, config), 0.92, "artist_from_source_collection"

    source_name = source.name
    year_trimmed = re.sub(r"\b(?:19|20)\d{2}\b", "", source_name).strip(" -_.")
    if year_trimmed and year_trimmed != source_name:
        norm = normalize(year_trimmed)
        if norm in normalized_collectors:
            pass
        elif norm in config.artist_aliases:
            return config.artist_aliases[norm], 0.96, "artist_from_source_alias"
        elif norm in reference.artist_precedent:
            return reference.artist_precedent[norm], 0.82, "artist_from_source_precedent"

    # Final fallback: mark clearly as collector-style if applicable.
    # Always try to strip known suffixes (" Collection", " Clips" etc.) even here.
    raw_fallback = next((candidate for candidate in candidates if not is_generic_source_folder(candidate)), source.name)
    stripped_fallback = strip_source_artist_suffix(raw_fallback) or raw_fallback
    if is_generic_source_folder(stripped_fallback):
        stripped_fallback = raw_fallback
    reason = "artist_from_collector_folder" if is_collector_folder(source.name) or is_collector_folder(source.parent.name) else "artist_low_confidence_source_folder"
    conf = 0.35 if "collector" in reason else 0.45
    return title_case_words(stripped_fallback, config.preserve_tokens), conf, reason


def date_prefixed_title(stem: str) -> str:
    match = DATE_PREFIX_RE.match(stem)
    return match.group(1).strip(" -_.") if match else ""


def artist_compact_date_prefix(stem: str) -> Optional[Tuple[str, str]]:
    """Return (artist_token, title_rest) if stem matches 'Artist 210704 Title...' style."""
    match = COMPACT_DATE_ARTIST_RE.match(stem)
    if match:
        artist_token = match.group(1).strip()
        title_rest = match.group(3).strip()
        if artist_token and title_rest:
            return artist_token, title_rest
    return None


def split_artist_and_title(stem: str, source: Path, config: Config, reference: ReferenceData) -> Tuple[str, str, float, str]:
    source_artist, source_confidence, source_reason = source_context_artist(source, config, reference)
    dated_title = date_prefixed_title(stem)
    if dated_title:
        return source_artist, dated_title, source_confidence, source_reason + "_date_prefixed"

    # Handle the very common collector upload pattern: "ArtistYYMMDD Title..." or "Artist YYYYMMDD Title..."
    # This is the dominant style in the Akiryo "Audio Collection" batch (e.g. "Mai 210704 ...", "Sinia 210422 ...")
    compact = artist_compact_date_prefix(stem)
    if compact:
        raw_artist, title_rest = compact
        is_collector_source = "collector" in source_reason or "artist_from_collector" in source_reason or source_reason.startswith("artist_from_source_collection") or source_reason.startswith("artist_low_confidence_source_folder")

        prefix_detection = detect_characters(clean_title(raw_artist, config), reference)
        if is_collector_source:
            if looks_like_artist_prefix(raw_artist, config, reference, is_collector_source=True):
                artist = canonical_artist(raw_artist, config, reference)
                return artist, title_rest, 0.96, "artist_from_filename_compact_date_over_collector"
            else:
                # Still prefer the filename token over the collector folder name even if weak
                artist = canonical_artist(raw_artist, config, reference)
                return artist, title_rest, 0.82, "artist_from_filename_compact_date_over_collector;weak"
        else:
            # Non-collector: still honor explicit artist-like short prefix + compact date
            if not prefix_detection.characters or is_known_artist_text(raw_artist, config, reference):
                artist = canonical_artist(raw_artist, config, reference)
                return artist, title_rest, 0.93, "artist_from_filename_compact_date"

    separators = [r"\s+-\s+", r"\s+--\s+", r"\s+_\s+"]
    for sep in separators:
        parts = re.split(sep, stem, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            raw_artist = parts[0].strip()
            prefix_detection = detect_characters(clean_title(raw_artist, config), reference)

            is_collector_source = "collector" in source_reason or source_reason.startswith("artist_from_source_collection") or source_reason.startswith("artist_low_confidence_source_folder")

            if is_collector_source:
                if not prefix_detection.characters and looks_like_artist_prefix(raw_artist, config, reference, is_collector_source=True):
                    artist = canonical_artist(raw_artist, config, reference)
                    return artist, parts[1].strip(), 0.97, "artist_from_filename_over_collector"
                else:
                    return source_artist, stem, source_confidence, source_reason + ";character_or_weak_prefix_from_filename"

            if prefix_detection.characters and not is_known_artist_text(raw_artist, config, reference):
                return source_artist, stem, source_confidence, source_reason + ";character_prefix_from_filename"

            artist = canonical_artist(raw_artist, config, reference)
            return artist, parts[1].strip(), 0.98, "artist_from_filename"

    return source_artist, stem, source_confidence, source_reason


def canonical_artist(raw_artist: str, config: Config, reference: ReferenceData) -> str:
    known = known_artist_from_text(raw_artist, config, reference)
    if known:
        return known
    return title_case_words(raw_artist, config.preserve_tokens)


def remove_resolution_text(text: str) -> str:
    value = text
    value = re.sub(r"(?i)(?<![A-Za-z0-9])(?:480|720|1080|1440|2160|4320)\s*p\s*(?:30|60)?\s*fps\b", " ", value)
    value = re.sub(r"(?i)(?<![A-Za-z0-9])[48]\s*k\s*(?:30|60)?\s*fps\b", " ", value)
    value = re.sub(r"(?i)\b(?:30|60)\s*fps\b", " ", value)
    value = re.sub(r"(?i)\b(?:full\s*hd|uhd)\b", " ", value)
    value = re.sub(r"(?i)(?<![A-Za-z0-9])(?:480|720|1080|1440|2160|4320)\s*p\b", " ", value)
    value = re.sub(r"(?i)(?<![A-Za-z0-9])[48]\s*k\b", " ", value)
    value = re.sub(r"\[[^\]]*(?:\d{3,4}\s*p|[48]\s*k|hd|uhd)[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"\([^\)]*(?:\d{3,4}\s*p|[48]\s*k|hd|uhd)[^\)]*\)", " ", value, flags=re.I)
    return value


def clean_title(raw_title: str, config: Config) -> str:
    value = raw_title
    value = value.replace("&", " and ")
    value = remove_resolution_text(value)
    value = re.sub(r"(?i)\b(?:unwatermarked|unwatermarket|no\s*watermark|nowatermark)\b", " ", value)
    for junk in config.junk_tokens:
        if normalize(junk) in {"1080p", "720p", "1440p", "2160p", "4k", "8k", "uhd", "full hd"}:
            continue
        value = re.sub(r"\b" + re.escape(junk) + r"\b", " ", value, flags=re.I)

    # Remove known audio producer / sound engineer credits (e.g. "audiodude", "evilaudio", "multiaudio")
    # These are common in collector filenames but are not part of the actual clip title or artist.
    for credit in getattr(config, "audio_credits", ()):
        value = re.sub(r"\b" + re.escape(credit) + r"\b", " ", value, flags=re.I)

    value = re.sub(r"[\[\]\{\}\(\)]", " ", value)
    value = value.replace("_", " ")
    value = re.sub(r"([A-Za-z])(\d+)\b", r"\1 \2", value)
    value = apply_title_token_replacements(value, config)
    for token in config.preserve_tokens:
        spaced = re.sub(r"([A-Za-z])(\d+)\b", r"\1 \2", token)
        if spaced != token:
            value = re.sub(r"\b" + re.escape(spaced) + r"\b", token, value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,;:])", r"\1", value)
    value = value.strip(" -_.,;")
    # Do not force "Untitled" — let empty titles be handled gracefully upstream
    # so minimal files don't get labeled "Untitled" just because their only
    # descriptive token was a stripped audio credit.
    value = title_case_words(value or "", config.preserve_tokens)
    return remove_duplicate_leading_phrase(value)


def apply_title_token_replacements(text: str, config: Config) -> str:
    value = text
    for raw_key, replacement in sorted(config.title_token_replacements.items(), key=lambda item: -len(item[0])):
        pattern = r"\b" + r"\s+".join(re.escape(part) for part in raw_key.split()) + r"\b"
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value


def remove_duplicate_leading_phrase(title: str) -> str:
    if " - " not in title:
        return title
    leading, rest = title.split(" - ", 1)
    leading_norm = normalize(leading)
    rest_norm = normalize(rest)
    if not leading_norm or not rest_norm.endswith(leading_norm):
        return title

    leading_words = leading_norm.split()
    rest_words = rest.split()
    if len(rest_words) <= len(leading_words):
        return title
    trimmed_rest = " ".join(rest_words[:-len(leading_words)]).strip()
    if not trimmed_rest:
        return leading
    return f"{leading} - {trimmed_rest}".strip()


def recover_descriptive_title_from_stem(stem: str, artist: str, config: Config) -> str:
    """Last-resort recovery for ultra-minimal collector files.

    After artist extraction + audio credit stripping, some files have literally
    no title words left (e.g. "Mai 220310 audiodude.mp4"). This tries to salvage
    any remaining descriptive token (color, simple action, etc.) that is not
    a known audio credit and not the artist.
    """
    if not stem:
        return ""

    # Remove the artist we already confidently extracted
    value = stem
    if artist:
        # Remove the artist token (case-insensitive word)
        value = re.sub(r"\b" + re.escape(artist) + r"\b", " ", value, flags=re.I)

    # Remove compact dates (6 or 8 digits) that may still be present
    value = re.sub(r"\b\d{6,8}\b", " ", value)

    # Remove known audio credits so we never recover them as the "title"
    for credit in getattr(config, "audio_credits", ()):
        value = re.sub(r"\b" + re.escape(credit) + r"\b", " ", value, flags=re.I)

    # Also remove common junk
    for junk in getattr(config, "junk_tokens", ()):
        value = re.sub(r"\b" + re.escape(junk) + r"\b", " ", value, flags=re.I)

    # Tokenize and pick the first non-trivial remaining word
    tokens = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9]*", value) if len(t) >= 3]
    if tokens:
        # Prefer earlier tokens that look like simple descriptors (colors, nude, etc.)
        return title_case_words(tokens[0], config.preserve_tokens)

    return ""


def probe_resolution(path: Path, ffprobe_path: str, extract_title: bool = False) -> Tuple[str, str, str]:
    """Probe resolution (and optionally embedded title metadata).

    Returns (resolution_label, reason, embedded_title or "").
    Title is only reliable for sparse-title collector files after caller sanity checks.
    """
    # Video stream for dimensions
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except FileNotFoundError:
        return "", "ffprobe_not_found", ""
    except subprocess.TimeoutExpired:
        return "", "ffprobe_timeout", ""

    if proc.returncode != 0:
        return "", "ffprobe_failed", ""

    try:
        data = json.loads(proc.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return "", "ffprobe_unreadable", ""

    res_label = resolution_label(width, height) if width and height else ""

    embedded_title = ""
    if extract_title:
        try:
            # Try format tags or first stream tags for title
            cmd_title = [
                ffprobe_path, "-v", "error",
                "-show_entries", "format_tags=title:stream_tags=title",
                "-of", "json", str(path),
            ]
            proc_t = subprocess.run(cmd_title, capture_output=True, text=True, timeout=15, check=False)
            if proc_t.returncode == 0:
                tdata = json.loads(proc_t.stdout or "{}")
                fmt = tdata.get("format", {})
                tags = fmt.get("tags", {}) or {}
                if not tags:
                    streams = tdata.get("streams") or []
                    if streams:
                        tags = streams[0].get("tags", {}) or {}
                title = tags.get("title", "").strip()
                if title and len(title) < 120:  # sanity
                    embedded_title = title
        except Exception:
            pass

    reason = "" if (width and height) else "ffprobe_no_dimensions"
    return res_label, reason, embedded_title


def resolution_bucket(width: int, height: int) -> str:
    long_side = max(width, height)
    short_side = min(width, height)
    if long_side >= 7600 or short_side >= 4320:
        return "8k"
    if long_side >= 3800 or short_side >= 2160:
        return "4k"
    if short_side >= 1440:
        return "1440"
    if short_side >= 1080:
        return "1080"
    if short_side >= 720:
        return "720"
    if short_side >= 480:
        return "480"
    return f"{width}x{height}"


def resolution_label(width: int, height: int) -> str:
    bucket = resolution_bucket(width, height)
    return output_resolution_label(bucket)


def format_resolution_label(label: str, reference: ReferenceData) -> str:
    if not label:
        return ""
    bucket = resolution_label_bucket(label)
    learned = reference.naming_style.resolution_labels.get(bucket)
    return output_resolution_label(bucket, learned or label)


def detect_characters(title: str, reference: ReferenceData) -> CharacterDetection:
    normalized_title = normalize(title)
    matches: List[Tuple[int, str, str]] = []
    for alias_norm, canonical in sorted(
        reference.canonical_character_aliases.items(),
        key=lambda item: (-len(item[0].split()), -len(item[0]), item[0]),
    ):
        idx = f" {normalized_title} ".find(f" {alias_norm} ")
        if idx >= 0:
            matches.append((idx, alias_norm, canonical))

    characters: List[str] = []
    aliases: List[str] = []
    reasons: List[str] = []
    for idx, alias_norm, canonical in sorted(matches, key=lambda item: item[0]):
        if canonical in characters:
            continue
        if any(normalize(existing) == normalize(canonical) for existing in characters):
            continue
        if any(existing_alias == alias_norm for existing_alias in aliases):
            continue
        characters.append(canonical)
        aliases.append(alias_norm)
        reasons.append(f"{alias_norm}->{canonical}")

    if not characters:
        return CharacterDetection((), 0.0, "no_character_match", ())

    confidence = 0.96 if any(" " in alias or any(ch.isdigit() for ch in alias) for alias in aliases) else 0.91
    return CharacterDetection(tuple(characters), confidence, ";".join(reasons[:5]), tuple(aliases))


def strip_detected_characters_from_title(title: str, detection: CharacterDetection) -> str:
    if not detection.characters:
        return title
    value = title
    for alias_norm in sorted(detection.matched_aliases, key=lambda alias: (-len(alias.split()), -len(alias))):
        parts = alias_norm.split()
        if not parts:
            continue
        pattern = r"(?i)(?<![A-Za-z0-9])" + r"[\s._'-]+".join(alias_part_pattern(part) for part in parts) + r"(?:'s|s')?(?![A-Za-z0-9])"
        value = re.sub(pattern, " ", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+-\s+", " - ", value)
    value = re.sub(r"^\s*-\s*", " ", value)
    value = re.sub(r"\s*-\s*$", " ", value)
    value = re.sub(r"\s+-\s+(\d+\b)", r" \1", value)
    value = re.sub(r"\s+", " ", value).strip(" -_.,;")
    value = strip_orphan_connector_edges(value)
    if not value:
        return ""
    value = remove_duplicate_leading_phrase(value)
    return remove_duplicate_terminal_segment(value)


def alias_part_pattern(part: str) -> str:
    if part.endswith("s") and len(part) > 1:
        return re.escape(part[:-1]) + r"'?s"
    return re.escape(part)


def strip_orphan_connector_edges(title: str) -> str:
    value = title
    for _ in range(3):
        previous = value
        value = re.sub(r"(?i)^\s*(?:and|with|x)\b\s*(?:[-+,&]\s*)?", "", value)
        value = re.sub(r"(?i)^\s*[-+,&]\s*(?:and\b\s*)?", "", value)
        value = re.sub(r"(?i)\s*(?:[-+,&]\s*)?\b(?:and|with|x)\b\s*$", "", value)
        value = re.sub(r"\s+", " ", value).strip(" -_.,;")
        if value == previous:
            break
    return value


def remove_duplicate_terminal_segment(title: str) -> str:
    if " - " not in title:
        return title
    leading, trailing = title.rsplit(" - ", 1)
    trailing_norm = normalize(trailing)
    leading_norm = normalize(leading)
    if trailing_norm and contains_phrase(leading_norm, trailing_norm):
        return leading
    return title


def target_filename_for(artist: str, character_text: str, title: str, resolution: str, extension: str) -> str:
    # Gracefully omit the title segment when it is empty (common after stripping
    # audio credits from very minimal collector filenames like "... audiodude.mp4").
    # Avoid duplicating artist/character when they are the same (e.g. Sinia case).
    parts = [artist]
    if character_text and character_text != artist:
        parts.append(character_text)
    if title and title.strip():
        parts.append(title)
    return safe_filename(" - ".join(parts) + f" [{resolution}]{extension.lower()}")


def classify_title(title: str, config: Config, reference: ReferenceData) -> Tuple[str, float, str]:
    normalized_title = normalize(title)
    folder_scores: Dict[str, float] = {}
    reasons: List[str] = []

    for alias_norm, folder in config.folder_aliases.items():
        if contains_phrase(normalized_title, alias_norm) and folder_can_be_target(folder, config, reference):
            folder_scores[folder] = max(folder_scores.get(folder, 0), 0.97)
            reason = f"folder_alias:{alias_norm}->{folder}"
            if not folder_exists(folder, reference):
                reason += ":create_folder"
            reasons.append(reason)

    if folder_scores:
        best_folder, best_score = sorted(folder_scores.items(), key=lambda item: item[1], reverse=True)[0]
        return best_folder, best_score, ";".join(reasons[:4])

    character_matches: List[Tuple[int, str, str, float]] = []
    missing_character_folders: List[str] = []
    # Main mappings + learned (confirmed) franchises as soft signals
    all_char_mappings = dict(config.character_mappings)
    if reference.learned_franchises:
        for ck, f in reference.learned_franchises.items():
            all_char_mappings.setdefault(ck, f)
    for char_norm, folder in all_char_mappings.items():
        idx = f" {normalized_title} ".find(f" {char_norm} ")
        if idx < 0:
            continue
        if folder_can_be_target(folder, config, reference):
            score = 0.95 if " " in char_norm or any(ch.isdigit() for ch in char_norm) else 0.91
            character_matches.append((idx, char_norm, folder, score))
            folder_scores[folder] = max(folder_scores.get(folder, 0), score)
            reason = f"character:{char_norm}->{folder}"
            if not folder_exists(folder, reference):
                reason += ":create_folder"
            reasons.append(reason)
        elif folder not in missing_character_folders:
            missing_character_folders.append(folder)

    character_folders = []
    for _idx, _char_norm, folder, _score in sorted(character_matches, key=lambda item: item[0]):
        if folder not in character_folders:
            character_folders.append(folder)
    if len(character_folders) > 1:
        first_idx, first_char, first_folder, first_score = sorted(character_matches, key=lambda item: item[0])[0]
        return first_folder, min(0.93, first_score), "cross_franchise_first_character"
    if len(character_folders) == 1:
        folder = character_folders[0]
        return folder, folder_scores[folder], ";".join(reasons[:4])

    for token in title_tokens(title):
        if token in PRECEDENT_STOP_TOKENS:
            continue
        if " " not in token and len(token) < 4:
            continue
        precedents = reference.token_precedent.get(token)
        if not precedents:
            continue
        folder, count = sorted(precedents.items(), key=lambda item: item[1], reverse=True)[0]
        if count >= 2:
            folder_scores[folder] = max(folder_scores.get(folder, 0), min(0.89, 0.70 + count / 100))
            reasons.append(f"precedent:{token}->{folder}:{count}")

    if not folder_scores:
        if missing_character_folders:
            return "", 0.0, "missing_destination_folder:" + "|".join(sorted(missing_character_folders))
        return "", 0.0, "no_franchise_match"

    best_folder, best_score = sorted(folder_scores.items(), key=lambda item: item[1], reverse=True)[0]
    tied = [folder for folder, score in folder_scores.items() if abs(score - best_score) < 0.001]
    if len(tied) > 1:
        first = first_folder_by_title_order(normalized_title, tied, config)
        if first:
            return first, best_score - 0.02, "cross_franchise_first_character"
        return "", 0.0, "ambiguous_franchise:" + "|".join(sorted(tied))

    return best_folder, best_score, ";".join(reasons[:4])


def contains_phrase(normalized_text: str, normalized_phrase: str) -> bool:
    return f" {normalized_phrase} " in f" {normalized_text} "


def folder_exists(folder: str, reference: ReferenceData) -> bool:
    return normalize(folder) in reference.destination_folders


def folder_can_be_target(folder: str, config: Config, reference: ReferenceData) -> bool:
    return folder_exists(folder, reference) or config.allow_create_destination_folders


def first_folder_by_title_order(normalized_title: str, folders: Sequence[str], config: Config) -> str:
    best_index: Optional[int] = None
    best_folder = ""
    for char_norm, folder in config.character_mappings.items():
        if folder not in folders:
            continue
        idx = f" {normalized_title} ".find(f" {char_norm} ")
        if idx >= 0 and (best_index is None or idx < best_index):
            best_index = idx
            best_folder = folder
    return best_folder


def infer_folder_from_artist(artist: str, artist_conf: float, config: Config, reference: ReferenceData) -> Tuple[str, float, str]:
    """Use a confidently identified artist name as a signal for destination folder.

    This is a key accuracy improvement for collector/uploader clips whose titles are
    very generic after the artist prefix is removed (common with "ArtistDate Title..." files).
    It consults the same character_mappings and folder_aliases the user already maintains.
    """
    if not artist or artist_conf < 0.70:
        return "", 0.0, ""

    norm = normalize(artist)

    # Direct hit in character_mappings (e.g. "mai" -> "King of Fighters")
    if norm in config.character_mappings:
        folder = config.character_mappings[norm]
        if folder_can_be_target(folder, config, reference):
            score = 0.93 if artist_conf >= 0.90 else 0.85
            reason = f"artist_mapping:{norm}->{folder}"
            if not folder_exists(folder, reference):
                reason += ":create_folder"
            return folder, score, reason

    # Check folder_aliases directly on the artist name
    if norm in config.folder_aliases:
        folder = config.folder_aliases[norm]
        if folder_can_be_target(folder, config, reference):
            score = 0.91 if artist_conf >= 0.90 else 0.82
            reason = f"artist_folder_alias:{norm}->{folder}"
            if not folder_exists(folder, reference):
                reason += ":create_folder"
            return folder, score, reason

    # Check if the artist (or its canonical form) has strong precedent in one folder
    # (artist_precedent only stores display name today; we can still try normalized lookup)
    if norm in reference.artist_precedent:
        # We don't currently track per-artist folder counts, so this is a weak secondary signal.
        # Stronger artist→folder learning can be added later.
        pass

    return "", 0.0, ""


def get_xai_api_key(config: Config) -> str:
    """Resolve the xAI API key from environment or local key file.

    Priority:
    1. Environment variable (as configured in ai_api_key_env_var)
    2. r34_xai_key.txt file next to the main config file (for convenience)
    """
    import os
    from pathlib import Path

    # 1. Environment variable (recommended for production)
    env_var_name = config.ai_api_key_env_var or "XAI_API_KEY"
    key = os.environ.get(env_var_name, "").strip()
    if key:
        return key

    # 2. Local key file next to the config (convenience for this user)
    try:
        config_path = default_config_path()
        key_file = config_path.with_name("r34_xai_key.txt")
        if key_file.exists():
            content = key_file.read_text(encoding="utf-8").strip()
            if content:
                return content
    except Exception:
        pass

    return ""


def query_grok_for_character_franchise(
    character: str,
    title: str,
    config: Config,
) -> Tuple[str, float, str]:
    """Optional: Ask Grok (via xAI API) for clarification on an unknown character's franchise.

    This fulfills the request for AI-assisted classification when the script has
    no strong local signal for where a newly inferred or unmatched character belongs.
    - Requires XAI_API_KEY (or configured env var) in environment, or r34_xai_key.txt next to config.
    - Returns (suggested_folder, confidence, reason) or ("", 0.0, "") on failure.
    - Never auto-applies high confidence; always surfaces as suggestion for CSV review.
    - Uses a tight prompt for short, usable folder names.
    """
    import os
    import json as _json

    api_key = get_xai_api_key(config)
    if not config.use_ai_for_unknown_characters or not api_key:
        return "", 0.0, ""

    # Simple in-process cache to avoid hammering the API for the same character in one run
    cache_key = (character.lower(), (title or "")[:80].lower())
    if not hasattr(query_grok_for_character_franchise, "_cache"):
        query_grok_for_character_franchise._cache = {}
    if cache_key in query_grok_for_character_franchise._cache:
        return query_grok_for_character_franchise._cache[cache_key]

    prompt = (
        "You are helping a user organize adult animation/video game clips into folders "
        "by franchise (e.g. 'King of Fighters', 'Overwatch', 'Final Fantasy', 'Original Character').\n"
        f"Character name: {character}\n"
        f"Clip title/description: {title or '(none)'}\n\n"
        "Reply with ONLY the single best short folder name for the destination library. "
        "If it is an original or very obscure character with no clear franchise, reply exactly 'Original Character'. "
        "Do not explain, do not add punctuation, do not list alternatives."
    )

    url = "https://api.x.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.ai_model,
        "messages": [
            {"role": "system", "content": "You give short, precise franchise or game names for clip organization."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 20,
        "temperature": 0.2,
    }

    try:
        import requests
        print(f"[Grok AI] Making API call to xAI for '{character}' ...")
        resp = requests.post(url, headers=headers, json=payload, timeout=12)
        if resp.status_code != 200:
            return "", 0.0, f"ai_error:http_{resp.status_code}"
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "").strip()
        suggestion = _validate_grok_franchise_response(content)
        reason_tag = "ai_grok_clarification"
        if suggestion == "Original Character" and content and content.lower() not in {"original character"}:
            reason_tag = "ai_grok_fallback:invalid_response"
        if suggestion:
            result = (suggestion, 0.78, f"{reason_tag}:{config.ai_model}")
            query_grok_for_character_franchise._cache[cache_key] = result
            return result
    except Exception as e:
        return "", 0.0, f"ai_error:{type(e).__name__}"

    return "", 0.0, ""


def _validate_grok_franchise_response(content: str) -> str:
    """Strict validator for Grok responses.

    Rejects: empty, multiline, overlong, hedged language, punctuation, or
    non-canonical replies. Safely falls back to 'Original Character' with
    a tagged reason so the decision remains fully auditable.
    """
    if not content:
        return "Original Character"
    c = content.strip()
    if "\n" in c or len(c) > 60:
        return "Original Character"
    low = c.lower()
    if any(bad in low for bad in ["i think", "maybe", "perhaps", "could be", "not sure", "unknown"]):
        return "Original Character"
    if any(p in c for p in ".,!?;:()[]\"'"):
        return "Original Character"
    if len(c.split()) > 4:
        return "Original Character"
    # Accept clean short names
    return c.strip(" .,'\"")


def detect_content_review(texts: Sequence[str], config: Config) -> Tuple[str, ...]:
    if not config.content_review_terms:
        return ()
    haystack = normalize(" ".join(text for text in texts if text))
    if not haystack:
        return ()

    matches: List[str] = []
    for category, terms in config.content_review_terms.items():
        for term in terms:
            term_norm = normalize(term)
            if term_norm and contains_phrase(haystack, term_norm):
                matches.append(f"{category}:{term}")
    return tuple(matches)


def safe_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]', "", name)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.rstrip(". ")
    # Avoid forcing "Untitled" here — higher-level logic (target_filename_for)
    # already handles omission of empty title segments gracefully.
    return value or ""


def unique_plan_paths(output_dir: Path, run: str) -> Tuple[Path, Path]:
    return output_dir / f"r34_preview_{run}.csv", output_dir / f"r34_preview_{run}.md"


def analyze_file(path: Path, source: Path, config: Config, reference: ReferenceData) -> Dict[str, str]:
    original_name = path.name
    stem = strip_leading_index(path.stem)
    artist, raw_title, artist_conf, artist_reason = split_artist_and_title(stem, source, config, reference)
    full_title = clean_title(raw_title, config)
    content_review_matches = detect_content_review((original_name, stem, raw_title, full_title), config)
    character_detection = detect_characters(full_title, reference)

    # When no known character matched, infer a plausible new one from the title,
    # use the detected name as-is, and add it to the live reference so the
    # current preview run treats it consistently (title stripping, etc.).
    # This builds the character database on the fly for previously unseen characters.
    if not character_detection.characters:
        inferred = infer_unmatched_character(raw_title or full_title, reference, config)
        if inferred:
            add_canonical_character_aliases(reference.canonical_character_aliases, inferred)
            # Re-detect so stripping and confidence logic pick it up with the new alias
            character_detection = detect_characters(full_title, reference)
            if not character_detection.characters:
                # Final fallback: use the raw inferred name directly
                character_detection = CharacterDetection(
                    (inferred,),
                    0.68,
                    "unmatched_character_inferred",
                    (normalize(inferred),)
                )

    # For collector "ArtistDate Credit" style files where the artist prefix is the
    # only identifying name and no title content survived stripping, also offer
    # the artist itself as the character. This helps cases like Sinia get a
    # character column even before the AI franchise step.
    if not character_detection.characters and artist:
        has_good_artist_prefix = (
            artist_conf >= 0.80 and
            ("filename" in artist_reason or "compact_date" in artist_reason)
        )
        if has_good_artist_prefix:
            character_detection = CharacterDetection(
                (artist,),
                0.65,
                "artist_prefix_used_as_character",
                (artist.lower(),)
            )

    character_text = ", ".join(character_detection.characters)
    title = strip_detected_characters_from_title(full_title, character_detection)

    # Normalize empty/"Untitled" titles (can happen for minimal collector files whose
    # only descriptive token was a stripped audio credit). target_filename_for will
    # omit the title segment cleanly.
    if not title or title.lower() == "untitled":
        title = ""

    # For extremely minimal collector filenames (only artist + date + audio credit),
    # try one last time to recover a short descriptive token (e.g. "Nude", "Pink")
    # without ever re-introducing stripped audio credits.
    if not title:
        recovered = recover_descriptive_title_from_stem(stem, artist, config)
        if recovered:
            title = recovered

    extract_title = getattr(config, "extract_embedded_titles", False)
    resolution, probe_reason, embedded_title = probe_resolution(path, config.ffprobe_path, extract_title=extract_title)
    resolution = format_resolution_label(resolution, reference)

    # Use embedded title only for sparse titles after basic sanity
    if extract_title and embedded_title and (not title or len(title.strip()) < 3):
        # sanity: ignore if looks like junk or too long
        if len(embedded_title) > 3 and len(embedded_title) < 80 and not any(j in embedded_title.lower() for j in ["www.", "http", "xxx", "porn"]):
            title = embedded_title.strip()
    target_folder, folder_conf, folder_reason = classify_title(full_title, config, reference)

    # Artist-driven folder inference (new): when the title is generic (very common for
    # collector clips after artist prefix removal), use the confidently extracted artist
    # against the user's character_mappings / folder_aliases as a strong signal.
    # This is the main lever for reducing manual CSV edits on new/unseen clips.
    if not target_folder or folder_conf < 0.75:
        artist_folder, artist_fconf, artist_freason = infer_folder_from_artist(
            artist, artist_conf, config, reference
        )
        if artist_folder and artist_fconf > folder_conf:
            target_folder = artist_folder
            folder_conf = artist_fconf
            folder_reason = artist_freason

    # AI-assisted franchise clarification (Grok) for cases where we have a strong
    # artist from filename prefix (common in collector dumps) or a character,
    # but still no confident target folder.
    # This is the main path that should have fired for the Sinia clip.
    has_good_artist_from_filename = (
        artist and artist_conf >= 0.80 and
        ("filename" in artist_reason or "compact_date" in artist_reason)
    )

    if (not target_folder or folder_conf < 0.70) and (character_text or has_good_artist_from_filename):
        name_for_ai = character_text or artist
        title_for_ai = full_title or raw_title

        # Make the AI usage visible in the console
        print(f"[Grok AI] Querying for franchise of '{name_for_ai}' (from {'character' if character_text else 'artist filename prefix'}) ...")

        ai_folder, ai_conf, ai_reason = query_grok_for_character_franchise(
            name_for_ai, title_for_ai, config
        )

        if ai_folder:
            # Always use AI result for these low-info cases — this is how we
            # effectively use the info from Grok calls to produce target names.
            target_folder = ai_folder
            folder_conf = max(folder_conf, ai_conf, 0.80)
            folder_reason = ai_reason

            # If we used the artist name for the AI query and have no separate character,
            # populate character so the filename includes it (e.g. Sinia - Sinia).
            if not character_text and artist == name_for_ai:
                character_text = artist
                character_detection = CharacterDetection(
                    (artist,), 0.70, "artist_used_as_character_for_ai", (artist.lower(),)
                )

    if (
        "filename_prefix_preserved" in artist_reason
        and folder_reason.startswith("precedent:")
    ):
        target_folder = ""
        folder_conf = 0.0
        folder_reason = "probable_character_prefix;precedent_suppressed"

    reasons = [artist_reason]
    if probe_reason:
        reasons.append(probe_reason)
    reasons.append(folder_reason)
    if character_detection.characters:
        reasons.append("canonical_character:" + character_detection.reason)
    if content_review_matches:
        reasons.append("content_review:" + "|".join(content_review_matches[:8]))

    # Build structured confidence instead of simple min collapse.
    char_conf = character_detection.confidence if character_detection.characters else 0.0
    title_conf = 0.85 if title and title.strip() else 0.40  # sparse titles penalized but not killed
    res_conf = 0.95 if resolution else 0.60
    franchise_conf = folder_conf if target_folder else 0.0

    row_conf = RowConfidence(
        artist=artist_conf,
        character=char_conf,
        franchise=franchise_conf,
        title=title_conf,
        resolution=res_conf,
    )
    confidence = row_conf.final()

    # Legacy boosts for strong signals (preserve behavior)
    if target_folder and "artist_mapping" in folder_reason:
        confidence = max(confidence, min(artist_conf, 0.92))
        row_conf = RowConfidence(  # re-wrap for dict later if needed
            artist=artist_conf, character=char_conf, franchise=franchise_conf,
            title=title_conf, resolution=res_conf
        )
    if target_folder and "ai_grok" in folder_reason:
        confidence = max(confidence, min(0.90, artist_conf, 0.82))

    # Strong final fallback for high-confidence "artist from filename prefix" cases
    if not target_folder:
        is_strong_artist_character = (
            artist and character_text == artist and
            artist_conf >= 0.80 and
            ("filename" in artist_reason or "compact_date" in artist_reason)
        )
        if is_strong_artist_character:
            target_folder = "Original Character"
            franchise_conf = 0.60
            folder_reason = "artist_as_character_fallback:Original Character"
            row_conf = RowConfidence(
                artist=artist_conf, character=char_conf, franchise=franchise_conf,
                title=title_conf, resolution=res_conf
            )
            confidence = max(confidence, row_conf.final())
            print(f"[Fallback] No folder/AI signal for strong artist-character '{artist}' — defaulting to 'Original Character' to produce target filename.")

    status = "ready" if confidence >= config.confidence_threshold and target_folder and resolution else "unmatched"
    approved = "yes" if status == "ready" else "no"
    notes = ""

    if content_review_matches:
        status = "content_review"
        approved = "no"
        notes = "Held for content review: " + ", ".join(content_review_matches[:8])
    elif not target_folder:
        notes = "Review: no destination folder confidently matched."
    elif "artist_mapping" in folder_reason and artist_conf >= 0.85:
        # We got the folder primarily because we confidently identified the artist
        # and it maps to a known franchise in the user's config.
        if not notes:
            notes = f"Artist '{artist}' strongly implies folder via mapping."
    elif "ai_grok" in folder_reason:
        if not notes:
            ai_name = character_text or artist or "unknown"
            notes = f"Folder suggested by Grok AI for '{ai_name}'."
    elif "artist_as_character_fallback" in folder_reason:
        if not notes:
            notes = f"Defaulted to '{target_folder}' using strong artist-as-character signal from filename (no AI or mapping available)."
    elif not resolution:
        notes = "Review: ffprobe could not determine resolution."
    elif artist_conf < config.confidence_threshold:
        notes = "Review: artist inference is low confidence."

    target_filename = ""
    target_path = ""
    if target_folder and resolution:
        target_filename = target_filename_for(artist, character_text, title, resolution, path.suffix)
        effective_folder = target_folder
        if target_folder == "Original Character" and getattr(config, "original_character_subfoldering", False) and artist:
            effective_folder = f"Original Character/{artist}"
        target_path = str(config.destination_root / effective_folder / target_filename)

    conf_dict = row_conf.as_dict()
    return {
        "approved": approved,
        "source_path": str(path),
        "original_name": original_name,
        "artist": artist,
        "character": character_text,
        "character_confidence": f"{char_conf:.2f}" if char_conf > 0 else "",
        "character_reason": character_detection.reason,
        "clean_title": title,
        "resolution": resolution,
        "target_folder": target_folder,
        "target_filename": target_filename,
        "target_path": target_path,
        "confidence": f"{confidence:.2f}",
        "artist_confidence": f"{conf_dict['artist_confidence']:.2f}",
        "character_confidence_component": f"{conf_dict['character_confidence']:.2f}",
        "franchise_confidence": f"{conf_dict['franchise_confidence']:.2f}",
        "title_confidence": f"{conf_dict['title_confidence']:.2f}",
        "resolution_confidence": f"{conf_dict['resolution_confidence']:.2f}",
        "weighted_confidence": f"{conf_dict['confidence']:.2f}",
        "status": status,
        "reason": ";".join(reasons),
        "notes": notes,
    }


def write_csv(path: Path, rows: Sequence[Dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_COLUMNS})


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [col for col in REQUIRED_CSV_COLUMNS if col not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Plan is missing required columns: {', '.join(missing)}")
        rows: List[Dict[str, str]] = []
        for row in reader:
            normalized = dict(row)
            for column in CSV_COLUMNS:
                normalized.setdefault(column, "")
            rows.append(normalized)
        return rows


def naming_style_resolution_summary(reference: ReferenceData) -> str:
    buckets = reference.naming_style.learned_resolution_buckets
    if not buckets:
        return "No parseable reference labels found; using built-in defaults."
    parts = [
        f"{bucket} -> {reference.naming_style.resolution_labels.get(bucket, bucket)}"
        for bucket in buckets
    ]
    return ", ".join(parts)


def write_preview_summary(
    path: Path,
    source: Path,
    destination_root: Path,
    rows: Sequence[Dict[str, str]],
    reference: ReferenceData,
) -> None:
    total = len(rows)
    ready = sum(1 for row in rows if row["status"] == "ready")
    content_review = sum(1 for row in rows if row["status"] == "content_review")
    unmatched = sum(1 for row in rows if row["status"] != "ready")
    by_folder: Dict[str, int] = {}
    for row in rows:
        folder = row.get("target_folder") or "(unmatched)"
        by_folder[folder] = by_folder.get(folder, 0) + 1

    lines = [
        "# Rule34 Organizer Preview",
        "",
        f"- Source: `{source}`",
        f"- Destination root: `{destination_root}`",
        f"- Total videos: {total}",
        f"- Ready/approved: {ready}",
        f"- Held for content review: {content_review}",
        f"- Needs review: {unmatched}",
        f"- Reference filename samples: {reference.naming_style.sample_count}",
        f"- Output resolution labels: {naming_style_resolution_summary(reference)}",
        "",
        "## By Destination",
        "",
    ]
    for folder, count in sorted(by_folder.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {folder}: {count}")
    lines.extend(["", "## Review Rows", ""])
    for row in rows:
        if row["status"] != "ready":
            lines.append(f"- `{row['original_name']}` -> {row.get('reason', '')} {row.get('notes', '')}".strip())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def command_preview(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.dest_root:
        config = replace_config(config, destination_root=Path(args.dest_root).resolve())
    if args.ffprobe:
        config = replace_config(config, ffprobe_path=args.ffprobe)

    source = Path(args.source).resolve()
    if not source.exists() or not source.is_dir():
        raise SystemExit(f"Source folder does not exist or is not a directory: {source}")
    if not config.destination_root.exists() or not config.destination_root.is_dir():
        raise SystemExit(f"Destination root does not exist or is not a directory: {config.destination_root}")

    reference = build_reference_data(config.destination_root, config)
    files = discover_videos(source, config)
    rows = [analyze_file(path, source, config, reference) for path in files]

    # Collect new Grok-derived character -> franchise mappings for pending review
    new_learned: Dict[str, str] = {}
    for row in rows:
        reason = row.get("reason", "")
        if "ai_grok_clarification" in reason and row.get("target_folder"):
            char = row.get("character", "").strip()
            folder = row.get("target_folder", "").strip()
            if char and folder and folder != "Original Character":
                new_learned[normalize(char)] = folder
    if new_learned:
        pending_path = write_pending_learned_franchises(new_learned, config)
        if pending_path:
            print(f"New Grok-derived mappings written to pending file for review: {pending_path}")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else source
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path, md_path = unique_plan_paths(output_dir, run_id())
    write_csv(csv_path, rows)
    write_preview_summary(md_path, source, config.destination_root, rows, reference)

    print(f"Preview complete: {len(rows)} video(s)")
    print(f"Reference filename samples: {reference.naming_style.sample_count}")
    print(f"Output resolution labels: {naming_style_resolution_summary(reference)}")
    print(f"CSV plan: {csv_path}")
    print(f"Summary:  {md_path}")
    print("Edit the CSV approved/status/target columns, then run apply.")
    return 0


def replace_config(config: Config, **updates: object) -> Config:
    data = {
        "destination_root": config.destination_root,
        "video_extensions": config.video_extensions,
        "ffprobe_path": config.ffprobe_path,
        "review_folder_name": config.review_folder_name,
        "content_review_folder_name": config.content_review_folder_name,
        "confidence_threshold": config.confidence_threshold,
        "allow_create_destination_folders": config.allow_create_destination_folders,
        "artist_aliases": config.artist_aliases,
        "folder_aliases": config.folder_aliases,
        "character_mappings": config.character_mappings,
        "canonical_character_aliases": config.canonical_character_aliases,
        "title_token_replacements": config.title_token_replacements,
        "content_review_terms": config.content_review_terms,
        "junk_tokens": config.junk_tokens,
        "preserve_tokens": config.preserve_tokens,
        "audio_credits": getattr(config, "audio_credits", ()),
        "known_collectors": getattr(config, "known_collectors", ()),
        "collection_folder_indicators": getattr(config, "collection_folder_indicators", ()),
        "use_ai_for_unknown_characters": getattr(config, "use_ai_for_unknown_characters", False),
        "ai_model": getattr(config, "ai_model", "grok-3"),
        "ai_api_key_env_var": getattr(config, "ai_api_key_env_var", "XAI_API_KEY"),
        "original_character_subfoldering": getattr(config, "original_character_subfoldering", False),
        "learned_franchises_file": getattr(config, "learned_franchises_file", "learned_character_franchises.json"),
        "extract_embedded_titles": getattr(config, "extract_embedded_titles", False),
    }
    data.update(updates)
    return Config(**data)


def approved_value(value: str) -> bool:
    norm = normalize(value)
    if norm in APPROVED_TRUE:
        return True
    if norm in APPROVED_FALSE:
        return False
    return False


def likely_same_clip(source: Path, target: Path) -> bool:
    if not target.exists() or not source.exists():
        return False
    try:
        if source.stat().st_size != target.stat().st_size:
            return False
    except OSError:
        return False
    return file_head_hash(source) == file_head_hash(target)


def file_head_hash(path: Path, limit: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()


def infer_source_root(rows: Sequence[Dict[str, str]]) -> Path:
    paths = [str(Path(row["source_path"]).resolve()) for row in rows if row.get("source_path")]
    if not paths:
        return Path.cwd()
    common = Path(os.path.commonpath(paths))
    return common if common.is_dir() else common.parent


def plan_run_id(plan: Path) -> str:
    match = re.search(r"(\d{8}-\d{6})", plan.stem)
    return match.group(1) if match else run_id()


def quarantine_path(source: Path, source_root: Path, review_folder_name: str, run: str) -> Path:
    review_root = source_root / review_folder_name / run
    review_root.mkdir(parents=True, exist_ok=True)
    candidate = review_root / source.name
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        suffixed = review_root / f"{source.stem} ({idx}){source.suffix}"
        if not suffixed.exists():
            return suffixed
        idx += 1


def content_review_path(source: Path, source_root: Path, content_review_folder_name: str, run: str) -> Path:
    review_root = source_root / content_review_folder_name / run
    review_root.mkdir(parents=True, exist_ok=True)
    candidate = review_root / source.name
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        suffixed = review_root / f"{source.stem} ({idx}){source.suffix}"
        if not suffixed.exists():
            return suffixed
        idx += 1


def apply_row(
    row: Dict[str, str],
    source_root: Path,
    run: str,
    review_folder_name: str,
    quarantine_unapproved: bool,
    content_review_folder_name: str = "_r34_content_review",
) -> Dict[str, str]:
    result = dict(row)
    source = Path(row.get("source_path", ""))
    target_text = row.get("target_path", "")
    status_raw = str(row.get("status", "")).strip().lower()
    status = normalize(row.get("status", ""))
    approved = approved_value(row.get("approved", ""))

    result["apply_result"] = ""
    result["apply_message"] = ""

    if status_raw == "content_review" or status == "content review":
        if not source.exists():
            result["apply_result"] = "missing_source"
            result["apply_message"] = str(source)
            return result
        dest = content_review_path(source, source_root, content_review_folder_name, run)
        shutil.move(str(source), str(dest))
        result["apply_result"] = "held_content_review"
        result["apply_message"] = str(dest)
        return result

    if not approved:
        if quarantine_unapproved and source.exists():
            dest = quarantine_path(source, source_root, review_folder_name, run)
            shutil.move(str(source), str(dest))
            result["apply_result"] = "quarantined_unapproved"
            result["apply_message"] = str(dest)
        else:
            result["apply_result"] = "skipped_unapproved"
            result["apply_message"] = "approved is not true/yes"
        return result

    if status_raw in BLOCKED_STATUSES or status in BLOCKED_STATUSES or status.startswith("blocked"):
        result["apply_result"] = "skipped_blocked"
        result["apply_message"] = f"status={row.get('status', '')}"
        return result

    if not source.exists():
        result["apply_result"] = "missing_source"
        result["apply_message"] = str(source)
        return result

    if not target_text:
        result["apply_result"] = "invalid_target"
        result["apply_message"] = "target_path is empty"
        return result

    target = Path(target_text)
    if target.exists():
        dest = quarantine_path(source, source_root, review_folder_name, run)
        shutil.move(str(source), str(dest))
        result["apply_result"] = "quarantined_duplicate" if likely_same_clip(dest, target) else "quarantined_conflict"
        result["apply_message"] = f"{dest} (target exists: {target})"
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    result["apply_result"] = "moved"
    result["apply_message"] = str(target)
    return result


def write_apply_log(plan: Path, rows: Sequence[Dict[str, str]], run: str) -> Path:
    log_path = plan.with_name(f"r34_apply_{run}.csv")
    fieldnames = CSV_COLUMNS + ["apply_result", "apply_message"]
    with log_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return log_path


def apply_progress_label(row: Dict[str, str]) -> str:
    original = row.get("original_name") or Path(row.get("source_path", "")).name or "(unknown file)"
    if row.get("apply_result") == "held_content_review" and row.get("apply_message"):
        return f"{original} -> {row['apply_message']}"
    target_name = row.get("target_filename") or Path(row.get("target_path", "")).name
    if target_name and target_name != original:
        target_folder = row.get("target_folder", "").strip()
        target_display = f"{target_folder}\\{target_name}" if target_folder else target_name
        return f"{original} -> {target_display}"
    return original


def command_apply(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plan = Path(args.plan).resolve()
    rows = read_csv(plan)
    source_root = Path(args.source_root).resolve() if args.source_root else infer_source_root(rows)
    run = plan_run_id(plan)
    applied: List[Dict[str, str]] = []
    total = len(rows)
    if total == 0:
        print("Apply progress: 0/0 (100%) - no rows to process", flush=True)
    for index, row in enumerate(rows, start=1):
        result = apply_row(
            row,
            source_root,
            run,
            config.review_folder_name,
            args.quarantine_unapproved,
            config.content_review_folder_name,
        )
        applied.append(result)
        percent = round((index / total) * 100) if total else 100
        outcome = result.get("apply_result", "unknown")
        label = apply_progress_label(result)
        print(f"Apply progress: {index}/{total} ({percent}%) - {outcome}: {label}", flush=True)

    log_path = write_apply_log(plan, applied, run)
    counts: Dict[str, int] = {}
    for row in applied:
        key = row.get("apply_result", "unknown")
        counts[key] = counts.get(key, 0) + 1
    print("Apply complete:")
    for key, count in sorted(counts.items()):
        print(f"  {key}: {count}")
    print(f"Apply log: {log_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview and apply Rule34 clip renames/moves.")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="Path to JSON config.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    preview = sub.add_parser("preview", help="Scan a source folder and write an editable CSV plan.")
    preview.add_argument("--source", required=True, help="Per-run source folder to scan recursively.")
    preview.add_argument("--dest-root", help="Override destination root.")
    preview.add_argument("--ffprobe", help="Override ffprobe executable path.")
    preview.add_argument("--output-dir", help="Where to write preview CSV/Markdown. Defaults to source folder.")
    preview.set_defaults(func=command_preview)

    apply = sub.add_parser("apply", help="Apply an approved preview CSV plan.")
    apply.add_argument("--plan", required=True, help="Preview CSV plan to apply.")
    apply.add_argument("--source-root", help="Override inferred source root for quarantine placement.")
    apply.add_argument(
        "--quarantine-unapproved",
        action="store_true",
        help="Move unapproved rows to review folder instead of leaving them in place.",
    )
    apply.set_defaults(func=command_apply)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
