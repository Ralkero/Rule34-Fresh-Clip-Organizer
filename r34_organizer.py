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
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


VERSION = "0.5.0"

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
    "variant_family",
    "variant_version",
    "variant_descriptors",
    "variant_credits",
    "variant_decision",
    "variant_reason",
    "variant_rank",
    "status",
    "reason",
    "notes",
]

VARIANT_CSV_COLUMNS = {
    "variant_family", "variant_version", "variant_descriptors", "variant_credits",
    "variant_decision", "variant_reason", "variant_rank",
}
OPTIONAL_CSV_COLUMNS = {"character", "character_confidence", "character_reason", "artist_confidence", "character_confidence_component", "franchise_confidence", "title_confidence", "resolution_confidence", "weighted_confidence"} | VARIANT_CSV_COLUMNS
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
    "down",
    "extra",
    "feet",
    "high",
    "full",
    "from",
    "girl",
    "hot",
    "loop",
    "long",
    "melon",
    "new",
    "night",
    "part",
    "res",
    "remake",
    "scene",
    "short",
    "the",
    "valentine",
    "vid",
    "vids",
    "ver",
    "version",
    "wife",
    "with",
    "without",
    "x",
}

CHARACTER_DESCRIPTOR_STOP_TOKENS = {
    "all",
    "angle",
    "angles",
    "anal",
    "backdoor",
    "beach",
    "bj",
    "blowjob",
    "cam",
    "camera",
    "cockrider",
    "compilation",
    "cowgirl",
    "deepthroat",
    "dildo",
    "dong",
    "doggy",
    "doggystyle",
    "facial",
    "feet",
    "footjob",
    "face",
    "handjob",
    "hj",
    "licking",
    "missionary",
    "multiaudio",
    "nude",
    "oral",
    "patron",
    "piledrived",
    "piledriver",
    "piledrivered",
    "pole",
    "prone",
    "quickie",
    "reverse",
    "ride",
    "riding",
    "sitting",
    "solo",
    "standing",
    "strapon",
    "spitroast",
    "thighjob",
    "titjob",
    "tribbing",
}

TECHNICAL_CHARACTER_STOP_TOKENS = {
    "animation",
    "animated",
    "cam",
    "camera",
    "classic",
    "concept",
    "commission",
    "cut",
    "exclusive",
    "feb",
    "january",
    "jan",
    "february",
    "march",
    "mar",
    "april",
    "apr",
    "june",
    "jun",
    "july",
    "jul",
    "august",
    "aug",
    "september",
    "sep",
    "october",
    "oct",
    "november",
    "nov",
    "december",
    "dec",
    "high",
    "long",
    "model",
    "official",
    "patron",
    "poll",
    "raffle",
    "raf",
    "realistic",
    "remake",
    "res",
    "sg",
    "showcase",
    "short",
    "sound",
    "seconds",
    "ver",
    "version",
    "without",
}

PRECEDENT_SUPPRESS_TOKENS = PRECEDENT_STOP_TOKENS | CHARACTER_DESCRIPTOR_STOP_TOKENS | TECHNICAL_CHARACTER_STOP_TOKENS | {
    "cake",
    "doll",
    "fuck",
    "fuk",
    "pog",
    "speshal",
    "subway",
    "turntable",
}
CHARACTER_ALIAS_DENYLIST = {
    "b",
    "dildo",
    "feet",
    "patron",
    "pov",
    "quickie",
    "seconds",
    "spitroast",
    "w",
    "whispers",
    "zero",
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
    "5 seconds or less",
    "6 9 seconds",
    "10 20 seconds",
    "longer animations",
    "duration unknown",
    "to sort",
    "unsorted",
    "video",
    "videos",
}

DURATION_BUCKET_FOLDER_NAMES = {
    "5 seconds or less",
    "6 9 seconds",
    "10 20 seconds",
    "longer animations",
    "duration unknown",
}

INTERNAL_ORGANIZER_FOLDER_NAMES = {
    "_r34_angle_variants",
    "_r34_content_review",
    "_r34_operation_logs",
    "_r34_review",
    "_r34_silent",
    "_r34_superseded_variants",
    "_r34_trimmed_for_review",
}

GENERIC_SUBFOLDER_CONTEXT_NAMES = {
    "bonus",
    "bonuses",
    "extra",
    "extras",
    "loop",
    "loops",
    "sound",
    "sounds",
    "sfx",
    "variants",
    "version",
    "versions",
}

SOURCE_COLLECTION_DATE_RE = r"(?:19|20)\d{2}(?:[-_. ]\d{1,2}(?:[-_. ]\d{1,2})?)?"
SOURCE_COLLECTION_RANGE_RE = (
    rf"(?:{SOURCE_COLLECTION_DATE_RE})"
    rf"(?:[-_. ]*(?:to|through|thru|until|up[-_. ]*to)[-_. ]*(?:{SOURCE_COLLECTION_DATE_RE}))?"
)
SOURCE_ORDINAL_RANGE_RE = r"\d{1,4}\s*(?:-|to|through|thru)\s*\d{1,4}"
SOURCE_ARTIST_SUFFIX_RE = re.compile(
    rf"(?i)[\s_.-]+(?:artist[\s_.-]+)?"
    rf"(?:collection|clips?|videos?|animations?|animation|packs?|batches|archives?|uploads?|downloads?)"
    rf"(?:[\s_.-]+(?:(?:from[\s_.-]+)?(?:{SOURCE_COLLECTION_RANGE_RE}|{SOURCE_ORDINAL_RANGE_RE})|"
    rf"(?:to|through|thru|until|up[-_. ]*to)[\s_.-]+(?:{SOURCE_COLLECTION_DATE_RE})))?\s*$"
)

SCENE_DESCRIPTOR_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), canonical)
    for pattern, canonical in (
        (r"\bpublic\s+hand\s*job\b", "Public Handjob"),
        (r"\bbreed\s+a\s+brat\b", "Breed A Brat"),
        (r"\brough\s+ride\b", "Rough Ride"),
        (r"\bpole\s+danc(?:e|ing)\b", "Pole Dancing"),
        (r"\breverse\s+cowgirl\b", "Reverse Cowgirl"),
        (r"\bjack[-\s]*o[-\s]*pose\b", "Jack-O-Pose"),
        (r"\bdeep\s*throat\b", "Deepthroat"),
        (r"\bhand\s*job\b", "Handjob"),
        (r"\bblow\s*job\b", "Blowjob"),
        (r"\boral\s+job\b", "Oral Job"),
        (r"\bcock\s*rider\b", "Cockrider"),
        (r"\bpile\s*driver(?:ed)?\b", "Piledriver"),
        (r"\bdoggy\s*style\b", "Doggystyle"),
        (r"\bon\s+top\b", "On Top"),
        (r"\bback\s*door\b", "Backdoor"),
        (r"\bmissionary\b", "Missionary"),
        (r"\bcowgirl\b", "Cowgirl"),
        (r"\bdoggy\b", "Doggy"),
        (r"\briding\b", "Riding"),
        (r"\bcreampie\b", "Creampie"),
        (r"\bdeepthroat\b", "Deepthroat"),
        (r"\bhandjob\b", "Handjob"),
        (r"\bblowjob\b", "Blowjob"),
        (r"\bbackdoor\b", "Backdoor"),
        (r"\bcockrider\b", "Cockrider"),
        (r"\bfacial\b", "Facial"),
        (r"\banal\b", "Anal"),
        (r"\boral\b", "Oral"),
        (r"\bnude\b", "Nude"),
        (r"\bBJ\b", "BJ"),
        (r"\bHJ\b", "HJ"),
        (r"\b69\b", "69"),
    )
)
SCENE_DESCRIPTOR_TOKEN_NORMS = frozenset(
    re.sub(r"[^a-z0-9]+", " ", token.lower()).strip()
    for _, canonical in SCENE_DESCRIPTOR_PATTERNS
    for token in re.findall(r"[A-Za-z0-9]+", canonical)
)

COMPACT_SCENE_DESCRIPTOR_SUFFIXES: Tuple[Tuple[str, str], ...] = (
    ("deepthroat", "Deepthroat"),
    ("blowjob", "Blowjob"),
    ("handjob", "Handjob"),
    ("thighjob", "Thighjob"),
    ("ridedildo", "Ride Dildo"),
    ("cockrider", "Cockrider"),
    ("missionary", "Missionary"),
    ("backdoor", "Backdoor"),
    ("creampie", "Creampie"),
    ("cowgirl", "Cowgirl"),
    ("doggy", "Doggy"),
    ("facial", "Facial"),
    ("riding", "Riding"),
    ("dildo", "Dildo"),
    ("ride", "Ride"),
    ("anal", "Anal"),
    ("oral", "Oral"),
    ("bj", "BJ"),
    ("hj", "HJ"),
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

DEFAULT_VARIANT_POLICY: Dict[str, Any] = {
    "enabled": True,
    "max_preferred_performances": 2,
    "duration_tolerance_seconds": 0.5,
    "duration_tolerance_percent": 2.0,
    "superseded_folder_name": "_r34_superseded_variants",
    "descriptor_aliases": {
        "no male audio": "NMA", "nomaleaudio": "NMA", "nma": "NMA",
        "standard": "Std", "default": "Std", "clothed": "Std", "std": "Std",
        "alternate": "Alt", "alt": "Alt", "point of view": "POV", "pov": "POV",
        "alternate angles": "Alt Angles", "alt angles": "Alt Angles", "altangles": "Alt Angles",
        "nude": "Nude", "bonus": "Bonus", "loop": "Loop", "barelegs": "Barelegs",
        "bare legs": "Barelegs", "no hat": "No Hat", "nohat": "No Hat",
        "no x ray": "No X-Ray", "noxray": "No X-Ray", "facesit": "Facesit",
        "facesitting": "Facesit", "pubes": "Pubes", "pubic hair": "Pubes",
        "full nude": "Nude", "nude version": "Nude", "nude ver": "Nude",
        "bra version": "Bra", "bra": "Bra", "no bra version": "No Bra", "no bra": "No Bra",
        "b": "Black", "black": "Black", "black version": "Black",
        "w": "White", "white": "White", "white version": "White",
        "front angle": "Front Angle", "alternate angle": "Alt Angle", "alt angle": "Alt Angle",
        "full audio version": "Full Audio", "full audio": "Full Audio", "full version": "Full",
        "nsfw": "NSFW", "sfw": "SFW", "loop ver": "Loop",
        "cream version": "Cream", "cream ver": "Cream", "creamy version": "Cream",
        "creamy ver": "Cream", "creampie": "Creampie", "cream": "Cream",
    },
    "negative_descriptor_aliases": ["nopubes", "no pubes", "no pubic hair"],
    "credit_aliases": {},
    "preferred_performances": {"global": [], "artists": {}, "characters": {}},
}


@dataclass(frozen=True)
class Config:
    destination_root: Path
    video_extensions: Tuple[str, ...]
    ffprobe_path: str
    review_folder_name: str
    content_review_folder_name: str
    silent_animations_folder_name: str
    confidence_threshold: float
    allow_create_destination_folders: bool
    artist_aliases: Dict[str, str]
    folder_aliases: Dict[str, str]
    character_mappings: Dict[str, str]
    canonical_character_aliases: Dict[str, str]
    title_token_replacements: Dict[str, str]
    filename_overrides: Dict[str, Dict[str, str]]
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
    auto_load_xai_key: bool
    # New production hardening options
    original_character_subfoldering: bool
    learned_franchises_file: str
    extract_embedded_titles: bool
    # New: when packs contain both individual "Cam X" angles and an "All Angles" compilation,
    # quarantine the individual cams to this subfolder inside the source (keep only All Angles for processing).
    angle_variants_folder_name: str
    variant_policy: Dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_VARIANT_POLICY)))

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
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    preserve_tokens = tuple(raw.get("preserve_tokens", []))
    character_mappings = {normalize(k): v for k, v in raw.get("character_mappings", {}).items()}
    canonical_character_aliases = {
        normalize(k): v for k, v in raw.get("canonical_character_aliases", {}).items()
    }
    for alias_norm, canonical in list(canonical_character_aliases.items()):
        if " " in alias_norm:
            compact = alias_norm.replace(" ", "")
            if len(compact) >= 5:
                canonical_character_aliases.setdefault(compact, canonical)
    for alias_norm in character_mappings:
        canonical_character_aliases.setdefault(alias_norm, title_case_words(alias_norm, preserve_tokens))

    variant_policy = json.loads(json.dumps(DEFAULT_VARIANT_POLICY))
    supplied_policy = raw.get("variant_policy", {})
    if isinstance(supplied_policy, dict):
        for key, value in supplied_policy.items():
            if key in {"descriptor_aliases", "credit_aliases"} and isinstance(value, dict):
                variant_policy[key].update(value)
            elif key == "preferred_performances" and isinstance(value, dict):
                for scope, entries in value.items():
                    if isinstance(entries, (dict, list)):
                        variant_policy[key][scope] = entries
            else:
                variant_policy[key] = value

    cfg = Config(
        destination_root=Path(raw["destination_root"]),
        video_extensions=tuple(ext.lower() for ext in raw.get("video_extensions", [".mp4"])),
        ffprobe_path=str(raw.get("ffprobe_path", "ffprobe")),
        review_folder_name=str(raw.get("review_folder_name", "_r34_review")),
        content_review_folder_name=str(raw.get("content_review_folder_name", "_r34_content_review")),
        silent_animations_folder_name=str(raw.get("silent_animations_folder_name", "_r34_silent")),
        confidence_threshold=float(raw.get("confidence_threshold", 0.9)),
        allow_create_destination_folders=bool(raw.get("allow_create_destination_folders", False)),
        artist_aliases={normalize(k): v for k, v in raw.get("artist_aliases", {}).items()},
        folder_aliases={normalize(k): v for k, v in raw.get("folder_aliases", {}).items()},
        character_mappings=character_mappings,
        canonical_character_aliases=canonical_character_aliases,
        title_token_replacements={normalize(k): v for k, v in raw.get("title_token_replacements", {}).items()},
        filename_overrides={
            normalize(k): {str(kk): str(vv) for kk, vv in (value or {}).items()}
            for k, value in raw.get("filename_overrides", {}).items()
            if isinstance(value, dict)
        },
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
        auto_load_xai_key=bool(raw.get("auto_load_xai_key", True)),
        original_character_subfoldering=bool(raw.get("original_character_subfoldering", False)),
        learned_franchises_file=str(raw.get("learned_franchises_file", "learned_character_franchises.json")),
        extract_embedded_titles=bool(raw.get("extract_embedded_titles", False)),
        angle_variants_folder_name=str(raw.get("angle_variants_folder_name", "_r34_angle_variants")),
        variant_policy=variant_policy,
    )
    # Attach the path the config was loaded from so key loading and other path-relative
    # features (like r34_xai_key.txt) can resolve correctly relative to the user's chosen config,
    # even when the organizer script is launched from a different directory (as the GUI does).
    object.__setattr__(cfg, "_loaded_config_path", path)
    return cfg


def default_config_path() -> Path:
    return Path(__file__).with_name("r34_config.json")


def load_learned_franchises(config: Config) -> Dict[str, str]:
    """Load manually confirmed learned franchises (safe, user-reviewed)."""
    fname = Path(config.learned_franchises_file).name
    loaded = getattr(config, "_loaded_config_path", None)
    if loaded:
        p = Path(loaded).with_name(fname)
    else:
        p = Path(config.learned_franchises_file)
        if not p.exists():
            p = default_config_path().with_name(fname)
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
    fname = Path(config.learned_franchises_file).with_suffix(".pending.json").name
    loaded = getattr(config, "_loaded_config_path", None)
    if loaded:
        p = Path(loaded).with_name(fname)
    else:
        p = Path(config.learned_franchises_file).with_suffix(".pending.json")
        if not p.parent.exists():
            p = default_config_path().with_name(fname)
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.update({k: v for k, v in new_mappings.items() if k not in existing})
    p.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def _is_learnable_franchise(char: str, folder: str) -> bool:
    """Only persist non-OC character->franchise mappings as learned signals.

    Apply of approved rows with these is treated as explicit human confirmation
    that the classification is satisfactory for future operations.
    """
    if not char or not folder:
        return False
    if folder.strip().lower() == "original character":
        return False
    return True


def write_learned_franchises(mappings: Dict[str, str], config: Config) -> Path:
    """Persist approved (character_norm -> franchise folder) mappings from successful applies.

    These are loaded by build_reference_data and used in classification/detection for
    future previews, improving accuracy without re-running Grok or heuristics.
    Keys are normalized; values preserve the folder name casing from the apply row.
    """
    if not mappings:
        return Path()
    fname = Path(config.learned_franchises_file).name
    loaded = getattr(config, "_loaded_config_path", None)
    if loaded:
        p = Path(loaded).with_name(fname)
    else:
        p = Path(config.learned_franchises_file)
        if not p.parent.exists() or not p.parent.is_dir():
            p = default_config_path().with_name(fname)
    existing: Dict[str, str] = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            existing = {normalize(k): v for k, v in raw.items()}
        except Exception:
            existing = {}
    changed = False
    for k, v in mappings.items():
        nk = normalize(k)
        if existing.get(nk) != v:
            existing[nk] = v
            changed = True
    if changed or not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        to_write = {k: v for k, v in existing.items()}
        p.write_text(json.dumps(to_write, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def revert_learned_franchise(char_norm: str, applied_franchise: str, pre_franchise: str, config: Config) -> bool:
    """Safely undo a single learned mapping written by a prior apply.

    Only reverts if the on-disk value still exactly matches what apply committed
    (defensive against manual edits to the learned json between apply and undo).
    If pre_franchise is falsy, the key is removed.
    Returns True if the file was modified.
    """
    if not char_norm:
        return False
    fname = Path(config.learned_franchises_file).name
    loaded = getattr(config, "_loaded_config_path", None)
    if loaded:
        p = Path(loaded).with_name(fname)
    else:
        p = Path(config.learned_franchises_file)
        if not p.exists():
            p = default_config_path().with_name(fname)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    current = data.get(char_norm, "")
    if current != applied_franchise:
        return False
    if pre_franchise:
        data[char_norm] = pre_franchise
    else:
        data.pop(char_norm, None)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def is_under_review_dir(path: Path, config: Config) -> bool:
    review_folders = {
        config.review_folder_name,
        config.content_review_folder_name,
        getattr(config, "silent_animations_folder_name", "_r34_silent"),
        getattr(config, "angle_variants_folder_name", "_r34_angle_variants"),
    }
    review_folders.update(INTERNAL_ORGANIZER_FOLDER_NAMES)
    review_folders.add((getattr(config, "variant_policy", {}) or {}).get("superseded_folder_name", "_r34_superseded_variants"))
    review_folders = {str(folder) for folder in review_folders if str(folder or "")}
    return any(part in review_folders for part in path.parts)


def discover_videos(source: Path, config: Config, show_progress: bool = False) -> List[Path]:
    """Recursively find video files, optionally showing a simple progress counter."""
    files: List[Path] = []
    count = 0
    last_print = 0

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if is_under_review_dir(path, config):
            continue
        if path.suffix.lower() in config.video_extensions:
            files.append(path)
            count += 1

            if show_progress and count % 50 == 0:
                print(f"\r  Scanning... found {count} videos so far", end="", flush=True)
                last_print = count

    if show_progress and count != last_print:
        print(f"\r  Scanning... found {count} videos so far", end="", flush=True)
        print()  # finish the line

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
    learned = load_learned_franchises(config) if config else {}
    if config:
        # learned first so that explicit config mappings/aliases take priority (per P3)
        for ck in learned:
            add_canonical_character_aliases(canonical_character_aliases, ck)
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
    ref_learned = learned or {}
    return ReferenceData(folders, artist_precedent, token_precedent, canonical_character_aliases, naming_style, learned_franchises=ref_learned)


def sanitize_character_candidate(candidate: str) -> str:
    """Remove title/camera descriptors from a candidate character segment.

    This prevents already-bad filenames in the destination library from teaching
    polluted aliases such as "Kaisa Beach Nude All Angles" or
    "Power Cowgirl, Power" to future preview runs.
    """
    if not candidate:
        return ""
    cleaned_parts: List[str] = []
    seen_norms: Set[str] = set()
    for raw_part in re.split(r"\s*,\s*|\s+&\s+", candidate):
        part = raw_part.strip(" -_.,;")
        if not part:
            continue
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9']*", part)
        if not tokens:
            continue
        keep_tokens: List[str] = []
        for token in tokens:
            token_norm = normalize(token)
            if token_norm in CHARACTER_DESCRIPTOR_STOP_TOKENS or token_norm in TECHNICAL_CHARACTER_STOP_TOKENS:
                break
            if token_norm.isdigit():
                break
            keep_tokens.append(token)
        if not keep_tokens:
            continue
        cleaned = " ".join(keep_tokens).strip(" -_.,;")
        cleaned_norm = normalize(cleaned)
        if cleaned_norm and cleaned_norm not in seen_norms:
            cleaned_parts.append(cleaned)
            seen_norms.add(cleaned_norm)
    return ", ".join(cleaned_parts)


def likely_canonical_character_from_title(title: str) -> str:
    if " - " not in title:
        return ""
    candidate = sanitize_character_candidate(title.split(" - ", 1)[0].strip())
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
    stop_words = set(PRECEDENT_STOP_TOKENS) | set(CHARACTER_DESCRIPTOR_STOP_TOKENS) | set(TECHNICAL_CHARACTER_STOP_TOKENS) | {
        "with", "and", "getting", "fucked", "pounded", "riding", "sucking",
        "fucking", "creampie", "anal", "pov", "bj", "from", "by", "in", "on",
        "hard", "deep", "fast", "slow", "audiodude", "blowjob", "fuk", "fuck"
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
    value = re.sub(r"^\s*\d+(?:\.\d+)*\.?\s+", "", value)
    value = re.sub(r"^\s*\d+\s*[-_. ]+\s*", "", value)
    return value.strip()


def is_generic_source_folder(name: str) -> bool:
    return normalize(name) in GENERIC_SOURCE_FOLDER_NAMES


def is_duration_bucket_folder(name: str) -> bool:
    normalized = normalize(name)
    if normalized in DURATION_BUCKET_FOLDER_NAMES:
        return True
    return bool(re.fullmatch(r"\d+\s+\d+\s+seconds|\d+\s+seconds(?:\s+or\s+less)?", normalized))


def is_internal_organizer_folder(name: str, config: Optional[Config] = None) -> bool:
    raw = str(name or "")
    normalized = normalize(raw)
    if raw.startswith("_r34_") or normalized.startswith("r34 "):
        return True
    configured = set(INTERNAL_ORGANIZER_FOLDER_NAMES)
    if config is not None:
        configured.update({
            config.review_folder_name,
            config.content_review_folder_name,
            getattr(config, "silent_animations_folder_name", "_r34_silent"),
            getattr(config, "angle_variants_folder_name", "_r34_angle_variants"),
            (getattr(config, "variant_policy", {}) or {}).get("superseded_folder_name", "_r34_superseded_variants"),
        })
    configured_norm = {normalize(item) for item in configured if item}
    return normalized in configured_norm


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
                return known, 0.96, "artist_from_source_alias_or_precedent"
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

    # Final fallback: the stem still contains the artist (common with space-separated
    # collector names like "MEGAERA 2025 Elf BJ..."). Strip it here so inference
    # and clean_title don't treat the artist token as part of a character name.
    title_for_return = strip_leading_artist_tokens(stem, source_artist)
    return source_artist, title_for_return or stem, source_confidence, source_reason


def canonical_artist(raw_artist: str, config: Config, reference: ReferenceData) -> str:
    known = known_artist_from_text(raw_artist, config, reference)
    if known:
        return known
    return title_case_words(raw_artist, config.preserve_tokens)


def remove_resolution_text(text: str) -> str:
    value = text

    # Very aggressive FPS removal - handles attached cases like "Nude60fps", "4K60", "Blowjob120fps"
    # 1. FPS with optional preceding number, possibly attached to previous word
    value = re.sub(r"(?i)(?<=[A-Za-z0-9])(?:30|60|120|144|240)?fps\b", " ", value)
    value = re.sub(r"(?i)\b(?:30|60|120|144|240)?fps\b", " ", value)

    # 2. Resolution + optional FPS (handles "1080p60", "4k60fps", "1440 60 fps")
    value = re.sub(r"(?i)(?:480|720|1080|1440|2160|4320)\s*p?\s*(?:30|60|120)?\s*fps?\b", " ", value)
    value = re.sub(r"(?i)[48]\s*k\s*(?:30|60|120)?\s*fps?\b", " ", value)

    # 3. Standalone fps mentions
    value = re.sub(r"(?i)\b(?:30|60|120|144|240)\s*fps?\b", " ", value)

    # 4. Other common video meta
    value = re.sub(r"(?i)\b(?:full\s*hd|uhd)\s*(?:30|60)?\s*fps?\b", " ", value)

    # Remove standalone resolution mentions (keep only for the final [1080P] tag)
    value = re.sub(r"(?i)(?<=[A-Za-z])(?:480|720|1080|1440|2160|4320)p\b", " ", value)
    value = re.sub(r"(?i)(?<=[A-Za-z])[48]k\b", " ", value)
    value = re.sub(r"(?i)(?<![A-Za-z0-9])(?:480|720|1080|1440|2160|4320)\s*p\b", " ", value)
    value = re.sub(r"(?i)(?<![A-Za-z0-9])[48]\s*k\b", " ", value)
    value = re.sub(r"(?i)\b(?:full\s*hd|uhd)\b", " ", value)

    # Remove any remaining resolution/FPS info inside brackets or parentheses
    value = re.sub(r"\[[^\]]*(?:\d{3,4}\s*p?|[48]\s*k|fps|hd|uhd)[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"\([^\)]*(?:\d{3,4}\s*p?|[48]\s*k|fps|hd|uhd)[^\)]*\)", " ", value, flags=re.I)

    # Final aggressive pass: remove common FPS framerates that often remain as orphaned numbers
    # (e.g. "Nude 60", "Blowjob 4K 60", "[60]", "Title 120")
    value = re.sub(r"(?i)(?<=[pPkK])\s*(?:30|60|120|144|240)\b", " ", value)
    value = re.sub(r"(?i)\b(?:30|60|120|144|240)\b(?=\s*[\]\)])", " ", value)
    value = re.sub(r"(?i)\b(?:30|60|120)\b(?=\s+(?:[A-Z]|\[|\(|\d))", " ", value)  # before capital letter or bracket

    return value


def strip_dates_and_years(text: str) -> str:
    """Remove years and date-like patterns from the descriptive title (not from artist prefixes)."""
    value = text
    # Collector-style compact dates and common date formats in title context.
    # Handle these before standalone years so month-year values like "06-2024"
    # do not leave an orphaned "06" in the scene title.
    value = re.sub(r"\b\d{1,2}[-_.](?:19|20)\d{2}\b", " ", value)
    value = re.sub(r"\b(?:19|20)\d{2}[-_.]\d{1,2}\b", " ", value)
    value = re.sub(r"\b(?:19|20)?\d{2}[-_.]?\d{1,2}[-_.]?\d{1,2}\b", " ", value)
    value = re.sub(r"\b\d{6,8}\b", " ", value)  # e.g. 210704

    # Standalone years
    value = re.sub(r"\b(?:19|20)\d{2}\b", " ", value)

    # Dates inside brackets/parentheses
    value = re.sub(r"\[[^\]]*?(?:19|20)\d{2}[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"\([^\)]*?(?:19|20)\d{2}[^\)]*\)", " ", value, flags=re.I)

    value = re.sub(r"\s+", " ", value).strip()
    return value


def strip_technical_title_labels(text: str) -> str:
    """Remove export/quality labels that are not meaningful scene descriptors."""
    value = text
    value = re.sub(r"(?i)(?<!\d)4\.0(?!\d)", " ", value)
    value = re.sub(r"(?i)\b(?:hi|high|low)\s*res(?:olution)?\b", " ", value)
    value = re.sub(r"(?i)\b(?:long|short)\s+(?:ver(?:sion)?|version)\b", " ", value)
    value = re.sub(r"(?i)\b(?:ver(?:sion)?|v)\s*\d+\b", " ", value)
    value = re.sub(r"(?i)\b(?:remake|unwatermarked|unwatermarket|no\s*watermark|nowatermark|no\s*wm|wm)\b", " ", value)
    value = re.sub(r"(?i)\b\d+(?:\.\d+)?\s*fps\b", " ", value)
    value = re.sub(r"(?i)\bfinal\b\s*$", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_.,;")


def expand_compact_scene_descriptor_token(token: str) -> str:
    """Split compact tokens like Widowbj or Commanderbj into name + descriptor.

    New collection dumps often omit separators in sparse filenames. Splitting
    only known descriptor suffixes lets the normal character detector infer the
    character while retaining useful scene descriptors in the final filename.
    """
    raw = str(token or "")
    if not raw or not re.fullmatch(r"[A-Za-z][A-Za-z0-9']*", raw):
        return raw

    base = re.sub(r"(?i)(?:480|720|1080|1440|2160|4320)p?$", "", raw)
    base = re.sub(r"(?i)[48]k$", "", base)
    base_norm = normalize(base)
    if not base_norm:
        return raw

    for suffix_norm, suffix_display in COMPACT_SCENE_DESCRIPTOR_SUFFIXES:
        descriptor_only = re.fullmatch(rf"(?i){re.escape(suffix_norm)}(?P<variant>\d+[a-z]?)?", base_norm)
        if descriptor_only:
            variant = descriptor_only.group("variant") or ""
            return f"{suffix_display} {variant}".strip()

        match = re.fullmatch(
            rf"(?P<prefix>[a-z0-9]{{3,}}){re.escape(suffix_norm)}(?P<variant>\d+[a-z]?)?",
            base_norm,
            flags=re.I,
        )
        if not match:
            continue

        prefix_norm = match.group("prefix")
        prefix_len = len(prefix_norm)
        prefix = base[:prefix_len].strip()
        if not prefix or normalize(prefix) in PRECEDENT_SUPPRESS_TOKENS:
            return raw
        variant = match.group("variant") or ""
        descriptor = f"{suffix_display} {variant}".strip()
        return f"{prefix} {descriptor}".strip()

    return raw


def expand_compact_scene_descriptors(text: str) -> str:
    if not text:
        return text

    def repl(match: re.Match[str]) -> str:
        return expand_compact_scene_descriptor_token(match.group(0))

    value = re.sub(r"\b[A-Za-z][A-Za-z0-9']*\b", repl, text)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_.,;")


def expand_known_character_variant_tokens(text: str, reference: ReferenceData) -> str:
    """Split compact known-character variants like Ahri1A or Ashebob2."""
    if not text or not reference or not reference.canonical_character_aliases:
        return text

    aliases = sorted(
        reference.canonical_character_aliases.items(),
        key=lambda item: (-len(item[0]), item[0]),
    )

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        token_norm = normalize(token)
        for alias_norm, canonical in aliases:
            compact_alias = alias_norm.replace(" ", "")
            if len(compact_alias) < 3 or not token_norm.startswith(compact_alias):
                continue
            suffix = token_norm[len(compact_alias):]
            if not re.fullmatch(r"\d+[a-z]?", suffix):
                continue
            suffix_display = suffix[:-1] + suffix[-1:].upper() if suffix[-1:].isalpha() else suffix
            return f"{canonical} {suffix_display}".strip()
        return token

    value = re.sub(r"\b[A-Za-z][A-Za-z0-9']*\b", repl, text)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -_.,;")


def normalized_filename_override_keys(path: Path, raw_title: str, file_full_title: str, full_title: str, config: Config) -> Tuple[str, ...]:
    candidates = [
        path.stem,
        strip_leading_index(path.stem),
        raw_title,
        file_full_title,
        full_title,
    ]
    keys: List[str] = []
    seen: Set[str] = set()
    for candidate in candidates:
        value = str(candidate or "")
        if not value:
            continue
        variants = [
            value,
            strip_technical_title_labels(strip_dates_and_years(remove_resolution_text(value))),
            clean_title(value, config),
        ]
        for variant in variants:
            key = normalize(variant)
            if key and key not in seen:
                keys.append(key)
                seen.add(key)
    return tuple(keys)


def find_filename_override(path: Path, raw_title: str, file_full_title: str, full_title: str, config: Config) -> Dict[str, str]:
    overrides = getattr(config, "filename_overrides", {}) or {}
    if not overrides:
        return {}
    for key in normalized_filename_override_keys(path, raw_title, file_full_title, full_title, config):
        override = overrides.get(key)
        if override:
            return dict(override)
    return {}


def is_variant_only_title(title: str) -> bool:
    value = normalize(title)
    return bool(re.fullmatch(r"(?:v\s*)?\d+[a-z]?|part\s+\d+[a-z]?", value))


def has_technical_date_or_quality_marker(text: str) -> bool:
    value = str(text or "")
    return bool(
        re.search(r"(?i)\b(?:hi|high|low)\s*res(?:olution)?\b", value)
        or re.search(r"\b(?:19|20)\d{2}\b", value)
        or re.search(r"\b\d{1,2}[-_.](?:19|20)\d{2}\b", value)
        or re.search(r"\b(?:19|20)\d{2}[-_.]\d{1,2}\b", value)
    )


def is_generic_context_variant_title(title: str) -> bool:
    value = normalize(title)
    if not value:
        return True
    value = re.sub(r"\b(?:v|ver|version|part)\s*\d+[a-z]?\b", " ", value)
    value = re.sub(r"\b\d+[a-z]?\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value in {"fuk", "fuck", "sex", "fucking", "bonk", "bonking"}


def strip_leading_artist_tokens(title: str, artist: str) -> str:
    """Aggressively remove the (already extracted) artist name and common year prefixes
    from the *title* portion. This fixes cases like "MEGAERA 2025 Elf BJ Nude..." where
    the artist token remains in the stem because there was no nice " - " separator.
    Prevents inference from turning "MEGAERA Elf" into a bogus "Megaera Elf" character.
    """
    if not title or not artist:
        return title
    value = title
    # Remove the exact artist (case-insensitive whole word)
    value = re.sub(r"\b" + re.escape(artist) + r"\b", " ", value, flags=re.I)
    # Common variants the collector might use (MEGAERA, Megaera, etc. already covered above)
    # Also strip years that often follow the artist in these packs
    value = re.sub(r"\b(19|20)\d{2}\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_.,;")
    return value


def clean_position_descriptors(title: str) -> str:
    """
    After a sex position word (doggy, missionary, etc.), keep only the first following number
    as a variant indicator. Remove extra numbers and common artifacts like 'Cam'.
    """
    if not title:
        return title

    value = title
    # Blowjob is treated as a generic position marker in the established naming style.
    value = re.sub(r"(?i)\bblow\s*job\b", " ", value)
    value = re.sub(r"(?i)\breverse\s*cowgirl\b", "Reverse Cowgirl", value)

    # Final aggressive removal of any remaining "Cam" tags and standalone numeric sequences *at the end only*.
    # Do not eat legitimate early numbers like "21 - Riding" (scene/episode style titles) or "Virus 2B MAX".
    # The .* $ was too broad; limit to trailing junk (fixes jessies_mom, max_quality, normalized_dup pre-existing tests).
    value = re.sub(r"(?i)\s*cam\s*\d+(?:[-\s]\d+)?\s*$", "", value)

    value = re.sub(r"\s+", " ", value).strip(" -_.,;")
    return value


def is_technical_residue_title(title: str) -> bool:
    tokens = normalize(title).split()
    return bool(tokens) and all(re.fullmatch(r"\d{1,4}[a-z]?", token) for token in tokens)


def extract_scene_descriptor_hint(title: str) -> str:
    """Return a meaningful action/position descriptor from a cleaned title.

    This protects preview names from collapsing to bland numeric variants when
    the library precedent table has not seen a descriptor yet.
    """
    value = str(title or "").strip()
    if not value:
        return ""
    for pattern, canonical in SCENE_DESCRIPTOR_PATTERNS:
        if pattern.search(value):
            return canonical
    return ""


def restore_scene_descriptor_hint(cleaned_title: str, original_title: str) -> str:
    descriptor = extract_scene_descriptor_hint(original_title)
    if not descriptor:
        return cleaned_title

    current = str(cleaned_title or "").strip(" -_.,;")
    descriptor_norm = normalize(descriptor)
    current_norm = normalize(current)
    if current_norm and contains_phrase(current_norm, descriptor_norm):
        return current

    if not current:
        return descriptor

    # Preserve useful existing variant markers, but attach them to the descriptor
    # instead of leaving a bare "1"/"V 2"/"Part 02" title.
    if re.fullmatch(r"(?i)(?:v\s*)?\d+", current) or re.fullmatch(r"(?i)part\s+\d+", current):
        return f"{current} {descriptor}" if normalize(original_title).startswith(normalize(current)) else f"{descriptor} {current}"

    return f"{current} {descriptor}".strip()


def strip_known_franchises_from_title(title: str, reference: ReferenceData, target_folder: str = "", config: Optional[Config] = None) -> str:
    """Remove known franchise/folder names from the descriptive title part."""
    if not title:
        return title
    value = title

    folders_to_strip = set()

    if target_folder:
        folders_to_strip.add(target_folder)

    if reference and reference.destination_folders:
        folders_to_strip.update(reference.destination_folders.values())

    if reference and reference.learned_franchises:
        folders_to_strip.update(reference.learned_franchises.values())

    # Also consider character_mappings values as possible franchise names
    if config and config.character_mappings:
        folders_to_strip.update(config.character_mappings.values())

    for folder in folders_to_strip:
        if folder:
            pattern = r"(?i)\b" + re.escape(folder) + r"\b"
            value = re.sub(pattern, " ", value)

    value = re.sub(r"\s+", " ", value).strip(" -_.,;")
    return value


def strip_franchises_preserving_characters(
    title: str, character_text: str, reference: ReferenceData, target_folder: str, config: Config
) -> str:
    value = str(title or "")
    markers: Dict[str, str] = {}
    for index, character in enumerate(sorted(character_parts(character_text), key=len, reverse=True)):
        marker = f"R34PROTECTEDCHAR{index}X"
        if character and character.lower() in value.lower():
            value = re.sub(re.escape(character), marker, value, flags=re.I)
            markers[marker] = character
    value = strip_known_franchises_from_title(value, reference, target_folder, config)
    for marker, character in markers.items():
        value = value.replace(marker, character)
    return value


def normalize_repeated_title_descriptors(title: str) -> str:
    """Compress repeated descriptor fragments left after character stripping."""
    if not title:
        return title
    value = re.sub(r"\s+", " ", title).strip(" -_.,;")
    for _ in range(3):
        previous = value
        value = re.sub(r"(?i)\b(nude|bj|cam|beach|missionary|doggy|cowgirl|all angles)\s+\1\b", r"\1", value)
        value = re.sub(
            r"(?i)\b(?P<phrase>(?:beach|missionary|doggy|cowgirl|cam)?\s*nude)\s+all angles\s+(?P=phrase)\b",
            r"\g<phrase> All Angles",
            value,
        )
        value = re.sub(
            r"(?i)\ball angles\s+(?P<phrase>(?:beach|missionary|doggy|cowgirl|cam)?\s*nude)\s+all angles\b",
            r"\g<phrase> All Angles",
            value,
        )
        value = re.sub(
            r"(?i)\b(?P<phrase>(?:doggy|missionary|cowgirl)?\s*nude\s+cam)\s+nude\s+cam\s+nude(?P<num>\s+\d+)?\b",
            lambda m: (m.group("phrase") + (m.group("num") or "")).strip(),
            value,
        )
        value = re.sub(r"\s+", " ", value).strip(" -_.,;")
        if value == previous:
            break
    return value


def strip_outlier_tokens(title: str, reference: ReferenceData, min_occurrence: int = 3) -> Tuple[str, List[str], float]:
    """
    Remove tokens from the title that appear very rarely (or never) in the existing library.
    Returns (cleaned_title, list_of_removed_tokens, confidence_that_removals_were_correct).
    High confidence → safe to auto-remove.
    Lower confidence → should be flagged for human review.
    """
    if not title or not reference.token_precedent:
        return title, [], 1.0

    tokens = re.findall(r"[A-Za-z0-9]+", title)
    if not tokens:
        return title, [], 1.0

    removed = []
    kept = []

    total_tokens_in_library = sum(sum(counts.values()) for counts in reference.token_precedent.values())

    for token in tokens:
        token_norm = normalize(token)
        if len(token_norm) < 2:
            kept.append(token)
            continue
        if token_norm in CHARACTER_DESCRIPTOR_STOP_TOKENS and token_norm != "blowjob":
            kept.append(token)
            continue
        if token_norm in SCENE_DESCRIPTOR_TOKEN_NORMS:
            kept.append(token)
            continue
        if token_norm in getattr(reference, "canonical_character_aliases", {}):
            kept.append(token)
            continue

        # Check how common this token is in the existing library
        precedents = reference.token_precedent.get(token_norm, {})
        occurrence = sum(precedents.values())

        # Also check if it's a known artist
        is_known_artist = token_norm in reference.artist_precedent

        # Special pattern detection for "Cam #-#" and similar camera/multicam tags
        cam_pattern = bool(re.match(r"^cam\s*\d+[-\s]?\d*$", token_norm, re.I))

        # Detect numeric sequences that look like extra parameters (e.g. after positions)
        numeric_junk = bool(re.match(r"^\d+[-\s]?\d*$", token_norm))

        if is_known_artist or occurrence >= min_occurrence:
            kept.append(token)
            continue

        # Calculate outlier score
        if total_tokens_in_library > 0:
            rarity = 1.0 - (occurrence / total_tokens_in_library)
        else:
            rarity = 1.0

        # Strongly boost for common artifact patterns
        if cam_pattern or numeric_junk:
            rarity = min(1.0, rarity + 0.55)

        if rarity >= 0.90:
            # High confidence this is extra metadata not seen in the library
            removed.append(token)
        elif rarity >= 0.70 or cam_pattern or numeric_junk:
            # Medium confidence - flag for review
            kept.append(token)
            removed.append(f"?{token}")
        else:
            kept.append(token)

    cleaned = " ".join(kept).strip()
    # Re-apply light cleanup
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_.,;")

    # Compute overall confidence in the cleaning decisions
    if not removed:
        confidence = 1.0
    else:
        # Rough confidence based on how rare the removed items were
        confidence = sum(0.95 if not t.startswith("?") else 0.6 for t in removed) / len(removed)

    # Return actual removed tokens without the ? marker
    real_removed = [t.lstrip("?") for t in removed]

    return cleaned, real_removed, round(confidence, 2)


def _variant_phrase_pattern(value: str) -> str:
    words = normalize(value).split()
    return r"(?<![A-Za-z0-9])" + r"[\s_.-]*".join(re.escape(word) for word in words) + r"(?![A-Za-z0-9])"


def variant_credit_aliases(config: Config) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for alias, canonical in (getattr(config, "variant_policy", {}) or {}).get("credit_aliases", {}).items():
        canonical_text = str(canonical).strip()
        if canonical_text:
            aliases[normalize(alias)] = canonical_text
            aliases[normalize(canonical_text)] = canonical_text
    return aliases


def extract_variant_metadata(texts: Sequence[str], config: Config, character_text: str = "") -> Dict[str, Any]:
    """Extract configured variant semantics before ordinary title cleanup can discard them."""
    policy = getattr(config, "variant_policy", {}) or {}
    sources = [str(text or "") for text in texts if str(text or "")]
    source = " | ".join(sources)
    descriptors: List[str] = []
    for alias, canonical in sorted((policy.get("descriptor_aliases") or {}).items(), key=lambda item: -len(str(item[0]))):
        if re.search(_variant_phrase_pattern(str(alias)), source, flags=re.I):
            value = str(canonical).strip()
            if value and value not in descriptors:
                descriptors.append(value)

    negative = any(
        re.search(_variant_phrase_pattern(str(alias)), source, flags=re.I)
        for alias in policy.get("negative_descriptor_aliases", [])
    )
    if negative:
        descriptors = [item for item in descriptors if normalize(item) != "pubes"]
    descriptor_norms = {normalize(item) for item in descriptors}
    for dominant, suppressed in {
        "no bra": {"bra"}, "alt angle": {"alt"}, "alt angles": {"alt"},
        "full audio": {"full"}, "nude": {"std"},
    }.items():
        if dominant in descriptor_norms:
            descriptors = [item for item in descriptors if normalize(item) not in suppressed]
            descriptor_norms = {normalize(item) for item in descriptors}

    sound_variant = re.search(r"(?i)\bsound[\s_.-]*variant[\s_.-]*(\d+)\b", source)
    if sound_variant:
        value = f"Sound V{int(sound_variant.group(1))}"
        if value not in descriptors:
            descriptors.append(value)

    credits: List[str] = []
    for alias_norm, canonical in variant_credit_aliases(config).items():
        if re.search(_variant_phrase_pattern(alias_norm), source, flags=re.I) and canonical not in credits:
            credits.append(canonical)

    version = ""
    match = None
    for candidate_source in sources:
        candidate_match = re.search(r"(?i)(?<![A-Za-z0-9])(?:scene|version|ver|v)[\s_.-]*0*(\d+)(?![\d.]|\s*fps)", candidate_source)
        if candidate_match:
            match = candidate_match
            break
    if match:
        version = f"V{int(match.group(1))}"
    elif character_text:
        for character in character_parts(character_text):
            tokens = [normalize(character), normalize(character).replace(" ", "")]
            for token in sorted(set(tokens), key=len, reverse=True):
                if not token:
                    continue
                for candidate_source in sources[:2]:
                    attached = re.search(r"(?i)(?<![A-Za-z0-9])" + re.escape(token) + r"[\s_.-]*0*(\d+)(?!\d|\s*fps)", normalize(candidate_source))
                    if attached:
                        version = f"V{int(attached.group(1))}"
                        break
                if version:
                    break
            if version:
                break
    return {"version": version, "descriptors": descriptors, "credits": credits}


def strip_variant_terms(title: str, metadata: Dict[str, Any], config: Config) -> str:
    value = str(title or "")
    policy = getattr(config, "variant_policy", {}) or {}
    aliases = list((policy.get("descriptor_aliases") or {}).keys()) + list(policy.get("negative_descriptor_aliases", []))
    aliases += list(variant_credit_aliases(config).keys())
    aliases.sort(key=lambda item: -len(str(item)))
    for alias in aliases:
        value = re.sub(_variant_phrase_pattern(str(alias)), " ", value, flags=re.I)
    value = re.sub(r"(?i)\bsound[\s_.-]*variant[\s_.-]*\d+\b", " ", value)
    value = re.sub(r"(?i)(?<![A-Za-z0-9])(?:scene|version|ver|v)[\s_.-]*\d+(?!\d)", " ", value)
    value = re.sub(r"\s*-\s*,\s*", " - ", value)
    value = re.sub(r"\s*,\s*-\s*", " - ", value)
    value = re.sub(r"(?:\s*-\s*){2,}", " - ", value)
    return re.sub(r"\s+", " ", value).strip(" -_.,;")


def compose_variant_title(base_title: str, metadata: Dict[str, Any]) -> str:
    parts: List[str] = []
    base = str(base_title or "").strip(" -_.,;")
    if base:
        parts.append(base)
    version = str(metadata.get("version") or "").strip()
    if version:
        parts.append(version)
    for item in list(metadata.get("descriptors") or []) + list(metadata.get("credits") or []):
        text = str(item).strip()
        if text and normalize(text) not in {normalize(existing) for existing in parts}:
            parts.append(text)
    if not parts:
        return ""
    if base and len(parts) > 1:
        return base + " - " + ", ".join(parts[1:])
    return ", ".join(parts)


def clean_title(raw_title: str, config: Config) -> str:
    value = raw_title
    value = value.replace("&", " and ")
    value = value.replace("+", " ")
    value = re.sub(r"\bw['’]?(?=\s)", "with", value)
    value = re.sub(r"'{2,}", "", value)
    value = re.sub(r"-{2,}", " ", value)
    value = strip_technical_title_labels(value)
    value = remove_resolution_text(value)
    value = strip_dates_and_years(value)
    value = strip_technical_title_labels(value)
    for junk in config.junk_tokens:
        if normalize(junk) in {"1080p", "720p", "1440p", "2160p", "4k", "8k", "uhd", "full hd"}:
            continue
        value = re.sub(r"\b" + re.escape(junk) + r"\b", " ", value, flags=re.I)

    # Remove known audio producer / sound engineer credits (e.g. "audiodude", "evilaudio", "multiaudio")
    # These are common in collector filenames but are not part of the actual clip title or artist.
    protected_variant_credits = set(variant_credit_aliases(config))
    for credit in getattr(config, "audio_credits", ()):
        if normalize(credit) in protected_variant_credits:
            continue
        value = re.sub(r"\b" + re.escape(credit) + r"\b", " ", value, flags=re.I)

    value = re.sub(r"[\[\]\{\}\(\)]", " ", value)
    value = value.replace("_", " ")
    value = re.sub(r"([A-Za-z])(\d+[A-Za-z])\b", r"\1 \2", value)
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


def meaningful_relative_subfolder_parts(path: Path, source: Path, config: Config, *, clean: bool = True) -> List[str]:
    """Return meaningful subfolder names between source root and file.

    Organizer/runtime folders and duration buckets are library structure, not scene
    identity. Keeping them out prevents output like "20 Seconds - Riding" and
    bogus learned characters such as "Seconds".
    """
    try:
        rel_parent = path.parent.relative_to(source)
    except ValueError:
        return []
    if not rel_parent.parts or rel_parent == Path("."):
        return []

    parts: List[str] = []
    seen_norms: Set[str] = set()
    for raw_part in rel_parent.parts:
        if not raw_part or raw_part.startswith("_"):
            continue
        if is_internal_organizer_folder(raw_part, config) or is_duration_bucket_folder(raw_part):
            continue
        stripped_source = strip_source_artist_suffix(raw_part)
        raw_context = strip_leading_index(stripped_source or raw_part)
        if (
            is_generic_source_folder(raw_context)
            or is_duration_bucket_folder(raw_context)
            or is_internal_organizer_folder(raw_context, config)
            or normalize(raw_context) in GENERIC_SUBFOLDER_CONTEXT_NAMES
        ):
            continue
        value = clean_title(raw_context, config) if clean else raw_context.strip(" -_.,;")
        if not value:
            continue
        cleaned_norm = normalize(value)
        if not cleaned_norm or cleaned_norm in seen_norms:
            continue
        parts.append(value)
        seen_norms.add(cleaned_norm)
    return parts


def relative_subfolder_context_title(path: Path, source: Path, config: Config) -> str:
    return " - ".join(meaningful_relative_subfolder_parts(path, source, config))


def format_subfolder_file_title(title: str) -> str:
    value = re.sub(r"\s+", " ", str(title or "")).strip(" -_.,;")
    if not value:
        return ""
    if re.match(r"(?i)^(?:loop|variant|version|ver|v\s*\d+|short|long)\b", value):
        value = re.sub(r"(?i)\s+with\s+", ", ", value, count=1)
    value = re.sub(r"\s*,\s*", ", ", value)
    return value.strip(" -_.,;")


def trim_leading_context_overlap(context_title: str, file_title: str) -> str:
    title = str(file_title or "").strip(" -_.,;")
    if not context_title or not title:
        return title

    context_words = set(normalize(context_title).split())
    if not context_words:
        return title

    for _ in range(4):
        tokens = title.split()
        if len(tokens) <= 1:
            break
        first_norm = normalize(tokens[0])
        if first_norm not in context_words:
            break
        title = " ".join(tokens[1:]).strip(" -_.,;")
    return title or file_title


def semantic_title_token(token: str) -> str:
    value = normalize(token)
    aliases = {
        "fuc": "fuck", "fucked": "fuck", "fucking": "fuck",
        "gettin": "get", "getting": "get", "havin": "have", "having": "have",
        "tiktoktrend": "tiktok", "reversecowgirl": "reverse cowgirl",
        "creampie": "cream", "creamy": "cream", "creamie": "cream",
        "an": "anal",
        "version": "ver", "angles": "angle", "secretaries": "secretary",
    }
    value = aliases.get(value, value)
    if value.endswith("s") and len(value) > 4 and value not in {"xmas", "anal"}:
        value = value[:-1]
    return value


def contextual_title_delta(context_title: str, file_title: str) -> str:
    """Return only meaningful file-title tokens not already represented by its folder."""
    context_keys = {semantic_title_token(token) for token in re.findall(r"[A-Za-z0-9']+", context_title)}
    file_tokens = re.findall(r"[A-Za-z0-9']+", file_title)
    file_keys = [semantic_title_token(token) for token in file_tokens]
    if not context_keys or not file_keys or not context_keys.intersection(file_keys):
        return file_title
    extras = [token for token, key in zip(file_tokens, file_keys) if key not in context_keys]
    return " ".join(extras).strip()


def merge_contextual_title_parts(context_title: str, file_title: str) -> str:
    context = re.sub(r"\s+", " ", str(context_title or "")).strip(" -_.,;")
    title = format_subfolder_file_title(file_title)
    if not context:
        return title
    if not title:
        return context

    context_norm = normalize(context)
    title_norm = normalize(title)
    if context_norm == title_norm or contains_phrase(context_norm, title_norm):
        return context
    if contains_phrase(title_norm, context_norm):
        return title
    title = trim_leading_context_overlap(context, title)
    title_norm = normalize(title)
    if context_norm == title_norm or contains_phrase(context_norm, title_norm):
        return context
    delta = contextual_title_delta(context, title)
    if not delta:
        return context
    return f"{context} - {delta}"


def title_context_contains_character(context_title: str, detection: "CharacterDetection") -> bool:
    context_norm = normalize(context_title)
    if not context_norm or not detection.characters:
        return False
    for character in detection.characters:
        char_norm = normalize(character)
        if char_norm and contains_phrase(context_norm, char_norm):
            return True
    for alias_norm in detection.matched_aliases:
        if alias_norm and contains_phrase(context_norm, alias_norm):
            return True
    return False


def title_context_is_exact_character(context_title: str, detection: "CharacterDetection") -> bool:
    context_norm = normalize(context_title)
    if not context_norm or not detection.characters:
        return False
    if any(context_norm == normalize(character) for character in detection.characters):
        return True
    return any(context_norm == alias_norm for alias_norm in detection.matched_aliases)


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

    # Filter out obvious FPS / technical junk that might have survived
    fps_like = re.compile(r"^(?:30|60|120|144|240)?fps?$", re.I)
    tokens = [t for t in tokens if not fps_like.match(t)]

    # Also drop standalone common framerates (60, 30, etc.) when they were likely FPS metadata
    fps_numbers = {"30", "60", "120", "144", "240"}
    tokens = [t for t in tokens if t not in fps_numbers]

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


def has_audio_stream(path: Path, ffprobe_path: str) -> bool:
    """Return True if the video file has at least one audio stream."""
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        return len(streams) > 0
    except Exception:
        # If probing fails, assume it has audio (safer than marking good files as silent)
        return True


def get_video_duration(path: Path, ffprobe_path: str) -> Optional[float]:
    """Return duration in seconds using ffprobe, or None on any failure."""
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        dur_str = (data.get("format") or {}).get("duration")
        return float(dur_str) if dur_str else None
    except Exception:
        return None


def get_video_frame_rate(path: Path, ffprobe_path: str) -> Optional[float]:
    """Return the first video stream's average frame rate, or None on failure."""
    cmd = [
        ffprobe_path, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate", "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        if proc.returncode != 0:
            return None
        value = ((json.loads(proc.stdout or "{}").get("streams") or [{}])[0]).get("avg_frame_rate", "")
        numerator, denominator = str(value).split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else None
    except Exception:
        return None


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
        if alias_norm in CHARACTER_ALIAS_DENYLIST:
            continue
        match = re.search(
            r"(?:^|\s)" + re.escape(alias_norm) + (r"(?:s)?" if not alias_norm.endswith("s") else "") + r"(?:\s|$)",
            normalized_title,
        )
        idx = match.start() if match else -1
        if idx >= 0:
            matches.append((idx, alias_norm, canonical))

    characters: List[str] = []
    aliases: List[str] = []
    reasons: List[str] = []
    for idx, alias_norm, canonical in sorted(matches, key=lambda item: item[0]):
        if canonical in characters:
            if alias_norm not in aliases:
                aliases.append(alias_norm)
            continue
        if any(normalize(existing) == normalize(canonical) for existing in characters):
            if alias_norm not in aliases:
                aliases.append(alias_norm)
            continue
        if any(contains_phrase(existing_alias, alias_norm) for existing_alias in aliases if existing_alias != alias_norm):
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


def canonicalize_detected_characters_in_title(title: str, detection: CharacterDetection) -> str:
    """Replace matched aliases in a scene title with their canonical display names."""
    value = str(title or "")
    replacements: List[Tuple[str, str]] = []
    reason_map = {}
    for item in str(detection.reason or "").split(";"):
        if "->" in item:
            alias, canonical = item.split("->", 1)
            reason_map[alias] = canonical
    for alias_norm in detection.matched_aliases:
        canonical = reason_map.get(alias_norm) or next((
            character for character in detection.characters
            if alias_norm in {normalize(candidate) for candidate in character_alias_candidates(character)}
        ), detection.characters[0] if len(detection.characters) == 1 else "")
        if canonical:
            replacements.append((alias_norm, canonical))
    markers: Dict[str, str] = {}
    for index, (alias_norm, canonical) in enumerate(sorted(replacements, key=lambda item: (-len(item[0].split()), -len(item[0])))):
        parts = alias_norm.split()
        if not parts:
            continue
        pattern = r"(?i)(?<![A-Za-z0-9])" + r"[\s._'-]+".join(alias_part_pattern(part) for part in parts) + r"(?:'s|s')?(?![A-Za-z0-9])"
        marker = f"R34CHARMARKER{index}X"
        updated = re.sub(pattern, marker, value)
        if updated != value:
            markers[marker] = canonical
            value = updated
    for marker, canonical in markers.items():
        value = value.replace(marker, canonical)
    return re.sub(r"\s+", " ", value).strip(" -_.,;")


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


def target_filename_for(artist: str, character_text: str, title: str, resolution: str, extension: str, *, preserve_variant_versions: bool = False) -> str:
    # Gracefully omit the title segment when it is empty.
    # Prevent artist/character duplication inside the title segment.
    artist = strip_source_artist_suffix(artist) or str(artist or "").strip()
    parts = [artist]
    if character_text and character_text != artist:
        parts.append(character_text)

    clean_title_part = title.strip() if title else ""

    # Strip leading artist or character name from the title to avoid duplication
    if clean_title_part:
        artist_norm = normalize(artist)
        char_norm = normalize(character_text) if character_text else ""
        title_norm = normalize(clean_title_part)
        character_name_norms = [normalize(part) for part in character_parts(character_text)] if character_text else []

        if title_norm and any(
            title_norm == char_part
            or (len(char_part.split()) > 1 and title_norm == char_part.split()[0])
            or (len(char_part.split()) > 1 and title_norm == char_part.split()[-1])
            for char_part in character_name_norms
        ):
            clean_title_part = ""
            title_norm = ""

        for name_norm in [artist_norm, char_norm]:
            if name_norm and title_norm.startswith(name_norm):
                # Remove the leading name + following separator
                name_pattern = r"[\s._'-]*".join(alias_part_pattern(part) for part in name_norm.split())
                clean_title_part = re.sub(r"(?i)^" + name_pattern + r"(?:'s|s')?[\s\-–—_.,:]+", "", clean_title_part).strip()
                title_norm = normalize(clean_title_part)
        for char_part in character_name_norms:
            words = char_part.split()
            if len(words) > 1 and title_norm.startswith(words[-1] + " "):
                clean_title_part = re.sub(r"(?i)^" + re.escape(words[-1]) + r"[\s\-–—_.,:]+", "", clean_title_part).strip()
                title_norm = normalize(clean_title_part)

    if clean_title_part:
        # Final safety net: aggressively strip any remaining FPS or date junk from the title segment
        clean_title_part = remove_resolution_text(clean_title_part)
        clean_title_part = strip_dates_and_years(clean_title_part)
        if not preserve_variant_versions:
            clean_title_part = strip_technical_title_labels(clean_title_part)

        # Extra pass specifically for orphaned numerical FPS values (30/60/120 etc.)
        clean_title_part = re.sub(r"(?i)\b(?:30|60|120|144|240)\b", " ", clean_title_part)

        clean_title_part = clean_title_part.strip(" -_.,;")
        if clean_title_part:
            if parts and is_variant_only_title(clean_title_part):
                parts[-1] = f"{parts[-1]} {clean_title_part}".strip()
            else:
                parts.append(clean_title_part)

    return safe_filename(" - ".join(parts) + f" [{resolution}]{extension.lower()}")


def is_suppressed_precedent_token(token: str) -> bool:
    token_norm = normalize(token)
    if not token_norm:
        return True
    words = token_norm.split()
    if token_norm in PRECEDENT_SUPPRESS_TOKENS:
        return True
    if any(word in PRECEDENT_SUPPRESS_TOKENS for word in words):
        return True
    if re.search(r"\bcam\s*\d+\b", token_norm):
        return True
    if re.fullmatch(r"(?:v\s*)?\d+", token_norm):
        return True
    return False


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
        if is_suppressed_precedent_token(token):
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
    if f" {normalized_phrase} " in f" {normalized_text} ":
        return True
    if normalized_phrase and not normalized_phrase.endswith("s"):
        return bool(re.search(r"(?:^|\s)" + re.escape(normalized_phrase) + r"s(?:\s|$)", normalized_text))
    return False


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


def infer_folder_from_detected_characters(
    character_text: str,
    config: Config,
    reference: ReferenceData,
) -> Tuple[str, float, str]:
    """Directly look up already-detected characters in mappings to find a franchise folder.

    This is especially useful for generic titles (e.g. "Cowgirl Nude All Angles") where
    classify_title() has little to work with, but we have strong character detections
    from earlier in the pipeline.

    Returns the strongest single franchise if one clearly dominates, otherwise ("", 0.0, "").
    """
    if not character_text:
        return "", 0.0, ""

    # Build the full set of mappings (config + learned)
    all_mappings = dict(config.character_mappings)
    if reference.learned_franchises:
        for ck, f in reference.learned_franchises.items():
            all_mappings.setdefault(ck, f)

    if not all_mappings:
        return "", 0.0, ""

    detected = [c.strip() for c in character_text.split(",") if c.strip()]
    if not detected:
        return "", 0.0, ""

    folder_scores: Dict[str, float] = {}
    reasons: List[str] = []

    for char in detected:
        char_norm = normalize(char)
        if char_norm in all_mappings:
            folder = all_mappings[char_norm]
            if folder_can_be_target(folder, config, reference):
                score = 0.94
                folder_scores[folder] = max(folder_scores.get(folder, 0), score)
                reason = f"detected_character:{char_norm}->{folder}"
                if not folder_exists(folder, reference):
                    reason += ":create_folder"
                reasons.append(reason)

    if not folder_scores:
        return "", 0.0, ""

    # If multiple characters point to different folders, be conservative
    if len(folder_scores) > 1:
        # Only accept if one folder has significantly stronger support
        sorted_folders = sorted(folder_scores.items(), key=lambda x: -x[1])
        best_folder, best_score = sorted_folders[0]
        second_score = sorted_folders[1][1] if len(sorted_folders) > 1 else 0
        if best_score > second_score + 0.15:
            return best_folder, best_score, ";".join(reasons[:3])
        return "", 0.0, "multiple_character_franchises"

    best_folder, best_score = next(iter(folder_scores.items()))
    return best_folder, best_score, ";".join(reasons[:3])


def get_xai_api_key(config: Config, config_path: Optional[Path] = None) -> str:
    """Resolve the xAI API key from environment or local key file.

    Priority:
    1. Environment variable (as configured in ai_api_key_env_var)
    2. r34_xai_key.txt next to the provided config_path (if given), otherwise
       next to the main config file resolved via default_config_path().

    The explicit config_path is important for the GUI when the user selects
    a custom r34_config.json in a different directory.
    """
    import os
    from pathlib import Path

    # 1. Environment variable (recommended for production)
    env_var_name = config.ai_api_key_env_var or "XAI_API_KEY"
    key = os.environ.get(env_var_name, "").strip()
    if key:
        return key

    # 2. Local key file next to the actual config being used
    try:
        if config_path is None:
            # Prefer the path the config was actually loaded from (set by load_config)
            config_path = getattr(config, "_loaded_config_path", None)
        if config_path is None:
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
    """Ask Grok (via xAI API) for clarification on a character's franchise.

    Used aggressively for cases with good character detections but weak title-based
    folder signals (very common with "All Angles" compilations under collector artists
    like Megaera). We now trigger earlier and with a more tolerant validator.
    - Requires XAI_API_KEY (or configured env var) + use_ai_for_unknown_characters=true.
    - Returns (suggested_folder, confidence, reason) or ("", 0.0, "") on failure.
    """
    import os
    import json as _json

    # Pass the loaded config path so the key file next to the user's chosen config is found
    # (critical when the GUI launches the script with --config pointing elsewhere).
    loaded_path = getattr(config, "_loaded_config_path", None)
    api_key = get_xai_api_key(config, config_path=loaded_path)
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

    # Debug: Show what we are actually asking Grok (sanitized)
    print(f"[Grok DEBUG] Prompt for '{character}':\n{prompt[:800]}{'...' if len(prompt) > 800 else ''}")

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
        raw_content = (choice.get("message") or {}).get("content", "").strip()

        # Debug: Show the raw answer we got back from Grok
        print(f"[Grok DEBUG] Raw response for '{character}': '{raw_content}'")

        suggestion = _validate_grok_franchise_response(raw_content)
        reason_tag = "ai_grok_clarification"
        if suggestion == "Original Character" and raw_content and raw_content.lower() not in {"original character"}:
            reason_tag = "ai_grok_fallback:invalid_response"
        if suggestion:
            result = (suggestion, 0.86, f"{reason_tag}:{config.ai_model}")
            query_grok_for_character_franchise._cache[cache_key] = result
            return result
    except Exception as e:
        return "", 0.0, f"ai_error:{type(e).__name__}"

    return "", 0.0, ""


def _validate_grok_franchise_response(content: str) -> str:
    """Validator for Grok responses (further relaxed for Megaera-style generic titles).

    We now accept more short, clean answers from Grok (including multi-word franchise names
    and "Original Character") so they can actually drive the target_folder and make rows ready.
    """
    if not content:
        return "Original Character"
    c = content.strip()
    if "\n" in c or len(c) > 100:
        return "Original Character"
    low = c.lower()
    if any(bad in low for bad in ["i think", "maybe", "perhaps", "could be", "not sure", "unknown", "probably", "i'm not sure"]):
        return "Original Character"
    # Only reject the most problematic punctuation; allow periods, commas, etc. for real names
    if any(p in c for p in "?;()[]\"'"):
        return "Original Character"
    if len(c.split()) > 8:
        return "Original Character"
    # Accept clean short names (strip trailing junk)
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


def deduplicate_target_filenames(rows: List[Dict[str, str]]) -> None:
    """Ensure every row gets a unique target_filename within this preview batch
    (and after manual edits in the Correction Tool) using the user's preferred
    numeric variant style instead of lazy "(N)" suffixes.

    Behavior:
    - Files whose cleaned names are identical are deduped by incrementing in
      the title descriptor slot (the token right before [RES]).
    - If the shared cleaned name already ends with a number (e.g. "... 1 [1080P]"),
      dups continue that sequence: first keeps "... 1", second becomes "... 2", etc.
      (Exactly: "Megaera - 2B - 1 [1080P].mp4" dup -> "... 2 [1080P].mp4")
    - If plain (e.g. "... Nude [1080P]" or "... Doggy [4K]"), first keeps the
      plain form, subsequent get " Nude 2", " Nude 3", ... or " Doggy 2" ... "Doggy 10".
    - A global second pass ensures that bumps never collide with other proposed
      names in the batch (e.g. a "BJ 1" dup bumping into an existing "BJ 2").
    - Never produces "(2)", "(3)", or any parenthesized suffix. Always uses the
      clean " <number>" style that matches the collector's own numbering.

    Mutates rows in-place and keeps target_path in sync.
    """
    from collections import defaultdict
    import re

    def _parse(fname: str):
        """Return (base, variant:int|None, res_tag, ext) or None."""
        if not fname:
            return None
        m = re.match(
            r"^(?P<base>.*?)(?:\s+(?P<var>\d+))?\s*(?P<res>\[[^\]]+\])(?P<ext>\.[^.]+)$",
            fname,
        )
        if not m:
            return None
        base = (m.group("base") or "").strip()
        var = int(m.group("var")) if m.group("var") else None
        return base, var, m.group("res"), m.group("ext")

    def _format(base: str, var: Optional[int], res: str, ext: str) -> str:
        if var is not None:
            return f"{base} {var} {res}{ext}"
        return f"{base}{res}{ext}"

    # Recognized variant families already carry explicit decisions. Numbering their
    # collisions would hide equivalence and pollute names for held/review rows.
    dedupe_rows = [row for row in rows if not row.get("variant_family")]

    # Phase 1: per original-collision-group bump (first claimant in scan order keeps its name)
    filename_groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in dedupe_rows:
        fname = row.get("target_filename", "").strip()
        if fname:
            filename_groups[fname].append(row)

    for fname, group in filename_groups.items():
        if len(group) <= 1:
            continue

        parsed = _parse(fname)
        if not parsed:
            # Robust fallback: locate res+ext and insert number before it (no parens ever)
            m = re.search(r"(\[[^\]]+\])(\.[^.]+)$", fname)
            if m:
                res, ext = m.groups()
                pre = fname[: m.start()].rstrip()
                for i, row in enumerate(group[1:], start=2):
                    new_name = f"{pre} {i} {res}{ext}"
                    row["target_filename"] = new_name
                    if row.get("target_path"):
                        row["target_path"] = str(Path(row["target_path"]).with_name(new_name))
            else:
                # Last-ditch: append _N before ext, still no ()
                p = Path(fname)
                for i, row in enumerate(group[1:], start=2):
                    new_name = f"{p.stem}_{i}{p.suffix}"
                    row["target_filename"] = new_name
                    if row.get("target_path"):
                        row["target_path"] = str(Path(row["target_path"]).with_name(new_name))
            continue

        base, existing_var, res, ext = parsed
        start_num = existing_var if existing_var is not None else 1

        for i, row in enumerate(group[1:], start=1):
            variant_num = start_num + i
            new_fname = _format(base, variant_num, res, ext)
            row["target_filename"] = new_fname
            if row.get("target_path"):
                p = Path(row["target_path"])
                row["target_path"] = str(p.with_name(new_fname))

    # Phase 2: global uniqueness sweep (catches any cross-group collisions created by bumps)
    seen: Dict[str, int] = {}
    for row in dedupe_rows:
        fname = row.get("target_filename", "").strip()
        if not fname:
            continue
        if fname not in seen:
            seen[fname] = 1
            continue
        # Need to bump this one
        parsed = _parse(fname)
        if not parsed:
            p = Path(fname)
            k = 2
            candidate = f"{p.stem} {k}{p.suffix}"
            while candidate in seen:
                k += 1
                candidate = f"{p.stem} {k}{p.suffix}"

            row["target_filename"] = candidate
            if row.get("target_path"):
                row["target_path"] = str(Path(row["target_path"]).with_name(candidate))
            seen[candidate] = 1
            continue

        base, var, res, ext = parsed
        k = (var or 1) + 1
        candidate = _format(base, k, res, ext)
        while candidate in seen:
            k += 1
            candidate = _format(base, k, res, ext)
        row["target_filename"] = candidate
        if row.get("target_path"):
            row["target_path"] = str(Path(row["target_path"]).with_name(candidate))
        seen[candidate] = 1


def analyze_file(path: Path, source: Path, config: Config, reference: ReferenceData) -> Dict[str, str]:
    original_name = path.name
    stem = strip_leading_index(path.stem)
    artist, raw_title, artist_conf, artist_reason = split_artist_and_title(stem, source, config, reference)
    raw_title = strip_leading_artist_tokens(raw_title, artist)
    raw_context_parts = meaningful_relative_subfolder_parts(path, source, config, clean=False)
    variant_metadata = extract_variant_metadata([stem, raw_title, *raw_context_parts], config)
    subfolder_context_title = relative_subfolder_context_title(path, source, config)
    file_full_title = clean_title(raw_title, config)
    file_full_title = expand_compact_scene_descriptors(file_full_title)
    file_full_title = expand_known_character_variant_tokens(file_full_title, reference)
    full_title = merge_contextual_title_parts(subfolder_context_title, file_full_title) if subfolder_context_title else file_full_title
    identity_file_title = strip_variant_terms(file_full_title, variant_metadata, config)
    identity_context_title = strip_variant_terms(subfolder_context_title, variant_metadata, config)
    identity_full_title = merge_contextual_title_parts(identity_context_title, identity_file_title) if identity_context_title else identity_file_title
    filename_override = find_filename_override(path, raw_title, file_full_title, full_title, config)
    content_review_matches = detect_content_review((original_name, stem, subfolder_context_title, raw_title, file_full_title, full_title), config)
    is_silent = not has_audio_stream(path, config.ffprobe_path)
    character_detection = detect_characters(identity_full_title, reference)

    override_character = (filename_override.get("character") or filename_override.get("characters") or "").strip()
    if override_character:
        add_canonical_character_aliases(reference.canonical_character_aliases, override_character)
        character_detection = CharacterDetection(
            tuple(character_parts(override_character)),
            0.99,
            "filename_override",
            tuple(normalize(part) for part in character_parts(override_character)),
        )

    # Define once early: is this a "collector" / generic artist (high-confidence folder-based
    # extraction or names like Megaera)? Used to make both character-folder and Grok/OC
    # logic more aggressive for exactly the cases the user is fighting.
    is_collector_artist = (
        artist_conf >= 0.75 and
        ("filename" in artist_reason or "compact_date" in artist_reason or "source" in artist_reason.lower())
    ) or any(x in (artist or "").lower() for x in ["megaera", "nsfwmegaera", "collector"])

    # When no known character matched, infer a plausible new one from the title,
    # use the detected name as-is, and add it to the live reference so the
    # current preview run treats it consistently (title stripping, etc.).
    # This builds the character database on the fly for previously unseen characters.
    if not character_detection.characters:
        inferred_source = identity_file_title or identity_full_title
        inferred = infer_unmatched_character(inferred_source, reference, config)
        if inferred:
            add_canonical_character_aliases(reference.canonical_character_aliases, inferred)
            # Re-detect so stripping and confidence logic pick it up with the new alias
            character_detection = detect_characters(identity_full_title, reference)
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
    if character_text:
        character_text = title_case_words(character_text, config.preserve_tokens)
        for canonical in character_detection.characters:
            if "(" in canonical or re.search(r"[a-z][A-Z]", canonical):
                character_text = character_text.replace(title_case_words(canonical, config.preserve_tokens), canonical)
        detailed_variant_metadata = extract_variant_metadata([stem, raw_title, *raw_context_parts], config, character_text)
        attached_version_candidate = detailed_variant_metadata.get("version", "")
    else:
        attached_version_candidate = ""
    if subfolder_context_title:
        file_title_without_character = strip_detected_characters_from_title(file_full_title, character_detection)
        file_title_without_character = strip_known_franchises_from_title(file_title_without_character, reference, config=config)
        canonical_context_title = canonicalize_detected_characters_in_title(subfolder_context_title, character_detection)
        context_norm_for_variant = normalize(canonical_context_title)
        context_already_action = any(token in context_norm_for_variant.split() for token in ("bonk", "bonking", "fuk", "fuck", "sex"))
        if is_generic_context_variant_title(file_title_without_character) and context_already_action:
            file_title_without_character = ""
        if is_variant_only_title(file_title_without_character) and title_context_is_exact_character(canonical_context_title, character_detection):
            title = f"{canonical_context_title} {file_title_without_character}".strip()
        else:
            title = merge_contextual_title_parts(canonical_context_title, file_title_without_character)
    else:
        title = strip_detected_characters_from_title(full_title, character_detection)

    # Apply additional meta stripping (FPS, dates/years) to the final title segment
    # so that only the resolution tag remains as technical meta-info in the filename.
    title = remove_resolution_text(title)
    title = strip_dates_and_years(title)
    title = strip_technical_title_labels(title)

    # Extra pass for numerical FPS values
    title = re.sub(r"(?i)\b(?:30|60|120|144|240)\b", " ", title)

    title = title.strip(" -_.,;")

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
            # Apply the same meta stripping to recovered titles so FPS/dates don't leak back in
            recovered = remove_resolution_text(recovered)
            recovered = strip_dates_and_years(recovered)
            title = recovered.strip(" -_.,;")

    if not subfolder_context_title and is_variant_only_title(title) and has_technical_date_or_quality_marker(raw_title):
        title = ""

    if "title" in filename_override or "clean_title" in filename_override:
        title = (filename_override.get("title") if "title" in filename_override else filename_override.get("clean_title", "")).strip(" -_.,;")

    # Clean sex position descriptors + trailing artifacts (e.g. "Doggy 2 1 Cam 3")
    title = clean_position_descriptors(title)
    title = normalize_repeated_title_descriptors(title)
    if is_technical_residue_title(title):
        title = ""

    # Detect outlier / extra parameters using the existing library as reference
    # (must happen after all title cleaning so we evaluate the final proposed title)
    outlier_removed = []
    outlier_conf = 1.0
    if reference and title and not subfolder_context_title:
        descriptor_source_title = title
        title, outlier_removed, outlier_conf = strip_outlier_tokens(title, reference)
        title = restore_scene_descriptor_hint(title, descriptor_source_title)
        title = normalize_repeated_title_descriptors(title)

    if not subfolder_context_title and is_variant_only_title(title) and has_technical_date_or_quality_marker(raw_title):
        title = ""

    extract_title = getattr(config, "extract_embedded_titles", False)
    resolution, probe_reason, embedded_title = probe_resolution(path, config.ffprobe_path, extract_title=extract_title)
    resolution = format_resolution_label(resolution, reference)

    # Early base confidence components (hoisted so they are always defined before
    # any conditional RowConfidence creations in the collector-artist / Grok / fallback paths).
    char_conf = character_detection.confidence if character_detection.characters else 0.0
    title_conf = 0.85 if title and title.strip() else 0.40
    res_conf = 0.95 if resolution else 0.60
    franchise_conf = 0.0   # will be updated whenever we set/adjust target_folder
    confidence = 0.0       # base value; will be properly computed later or via max() in early blocks

    # Use embedded title only for sparse titles after basic sanity
    if extract_title and embedded_title and (not title or len(title.strip()) < 3):
        # sanity: ignore if looks like junk or too long
        if len(embedded_title) > 3 and len(embedded_title) < 80 and not any(j in embedded_title.lower() for j in ["www.", "http", "xxx", "porn"]):
            title = embedded_title.strip()
    target_folder, folder_conf, folder_reason = classify_title(identity_full_title, config, reference)

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

    # Direct character-driven folder lookup (new high-ROI path for generic titles).
    # Once we have solid character detections, look them up directly in mappings
    # even if the cleaned title is very generic ("Cowgirl Nude", "Doggy Outfit 1", etc.).
    # This is the main fix for Megaera-style "All Angles" packs.
    #
    char_lookup_threshold = 0.70 if is_collector_artist else 0.80

    if not target_folder or folder_conf < char_lookup_threshold:
        char_folder, char_fconf, char_freason = infer_folder_from_detected_characters(
            character_text, config, reference
        )
        if char_folder:
            # For collector artists, be willing to use the character franchise even with moderate confidence.
            min_char_score = 0.82 if is_collector_artist else 0.88
            if char_fconf > folder_conf and char_fconf >= min_char_score:
                target_folder = char_folder
                folder_conf = max(folder_conf, char_fconf)
                folder_reason = char_freason
                if is_collector_artist:
                    print(f"[Character Folder] Using direct mapping for collector artist '{artist}': {char_folder}")

    # Explicit config character_mappings must always win over later OC/collector artist-as-char
    # or learned fallbacks (see test_explicit_config_mapping_is_not_overwritten).
    # Do this after the main char lookup so strong explicit drives target_folder.
    for ch in (character_detection.characters if character_detection else ()):
        chn = normalize(ch)
        if chn in config.character_mappings:
            target_folder = config.character_mappings[chn]
            folder_conf = max(folder_conf, 0.95)
            folder_reason = (folder_reason or "") + ";explicit_config_char_mapping_wins"
            break

    # AI-assisted franchise clarification (Grok) for cases where we have a strong
    # artist from filename prefix (common in collector dumps) or a character,
    # but still no confident target folder.
    # This is the main path that should have fired for the Sinia clip.
    has_good_artist_from_filename = (
        artist and artist_conf >= 0.80 and
        ("filename" in artist_reason or "compact_date" in artist_reason)
    )

    if config.use_ai_for_unknown_characters and (not target_folder or folder_conf < 0.85) and (character_text or has_good_artist_from_filename):
        # Be more aggressive about calling Grok for Megaera-style artists with detected
        # characters but weak folder signals (the most common remaining failure mode).
        name_for_ai = character_text or artist
        title_for_ai = identity_full_title or identity_file_title

        # Make the AI usage visible in the console
        print(f"[Grok AI] Querying for franchise of '{name_for_ai}' (from {'character' if character_text else 'artist filename prefix'}) ...")

        ai_folder, ai_conf, ai_reason = query_grok_for_character_franchise(
            name_for_ai, title_for_ai, config
        )

        if ai_folder:
            # Always use AI result for these low-info cases — this is how we
            # effectively use the info from Grok calls to produce target names.
            target_folder = ai_folder
            # Stronger boost so clean Grok answers (franchise or "Original Character")
            # more easily make the row ready for generic collector titles.
            folder_conf = max(folder_conf, ai_conf, 0.88)
            folder_reason = ai_reason
            if is_collector_artist:
                print(f"[Grok] Accepted for collector artist '{artist}': {ai_folder} (conf {ai_conf})")

            # If we used the artist name for the AI query and have no separate character,
            # populate character so the filename includes it (e.g. Sinia - Sinia).
            if not character_text and artist == name_for_ai:
                character_text = artist
                character_detection = CharacterDetection(
                    (artist,), 0.70, "artist_used_as_character_for_ai", (artist.lower(),)
                )

    # Enhanced collector artist handling for "Original Character" cases.
    # For artists like Megaera (collector/generic style), when we have a real detected
    # character (from Grok returning "Original Character" or from earlier detection),
    # create a usable target under "Original Character / <Artist>" with solid confidence.
    # This directly addresses the long/generic "All Angles" titles that were falling to unmatched.
    if is_collector_artist and character_text and (not target_folder or target_folder == "Original Character"):
        # Use the first (primary) detected character as the key under OC/Artist
        primary_char = character_text.split(",")[0].strip() if character_text else artist
        if primary_char and primary_char.lower() != artist.lower():
            target_folder = f"Original Character / {artist}"
            franchise_conf = 0.78
            folder_conf = max(folder_conf, 0.88)
            folder_reason = "collector_artist_original_character"
            # Rebuild character_detection to include the primary char so target_filename_for works nicely
            if primary_char not in [c.strip() for c in character_text.split(",")]:
                # Already have it from earlier detection in most cases
                pass
            row_conf = RowConfidence(
                artist=artist_conf, character=char_conf, franchise=franchise_conf,
                title=title_conf, resolution=res_conf
            )
            confidence = max(confidence, row_conf.final())
            print(f"[Collector OC] Using '{target_folder}' for detected character '{primary_char}' under collector artist '{artist}'")

    if (
        "filename_prefix_preserved" in artist_reason
        and folder_reason.startswith("precedent:")
    ):
        target_folder = ""
        folder_conf = 0.0
        folder_reason = "probable_character_prefix;precedent_suppressed"

    override_folder = (filename_override.get("target_folder") or filename_override.get("folder") or "").strip()
    if override_folder:
        target_folder = override_folder
        folder_conf = max(folder_conf, 0.99)
        folder_reason = (folder_reason + ";filename_override").strip(";")

    reasons = [artist_reason]
    if probe_reason:
        reasons.append(probe_reason)
    reasons.append(folder_reason)
    if character_detection.characters:
        reasons.append("canonical_character:" + character_detection.reason)
    if content_review_matches:
        reasons.append("content_review:" + "|".join(content_review_matches[:8]))

    # Update franchise confidence now that target_folder decisions are final.
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
        # Much stronger boost for Grok results so they can push generic-title rows over the
        # confidence threshold and produce ready target filenames.
        confidence = max(confidence, min(0.93, artist_conf, 0.88))

    reliable_folder_signal = (
        folder_conf >= 0.88
        or "explicit_config_char_mapping_wins" in folder_reason
        or "collector_artist_original_character" in folder_reason
        or "filename_override" in folder_reason
    )
    if target_folder and resolution and character_text and artist_conf >= 0.90 and char_conf >= 0.90 and reliable_folder_signal:
        confidence = max(confidence, config.confidence_threshold)

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
    elif is_silent:
        status = "silent"
        approved = "no"
        notes = "Silent animation (no audio stream detected)"
    elif outlier_removed:
        # Some tokens looked like extra parameters not seen in the user's existing library
        real_outliers = [t for t in outlier_removed if not t.startswith("?")]
        review_outliers = [t.lstrip("?") for t in outlier_removed if t.startswith("?")]

        if review_outliers:
            status = "review"
            approved = "no"
            notes = f"Review: possible extra parameters: {', '.join(review_outliers)} (low confidence removal)"
        elif real_outliers:
            # High confidence - we already removed them from the title
            if not notes:
                notes = f"Removed outlier tokens: {', '.join(real_outliers)}"
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

    # Strip known franchise/folder names from the title (they belong in the folder, not the filename)
    if target_folder or reference:
        title = strip_franchises_preserving_characters(title, character_text, reference, target_folder, config)
        title = normalize_repeated_title_descriptors(title)

    target_filename = ""
    target_path = ""
    if target_folder and resolution:
        filename_character_text = character_text
        context_contains_folder = bool(target_folder and contains_phrase(normalize(subfolder_context_title), normalize(target_folder)))
        if subfolder_context_title and not context_contains_folder and title_context_contains_character(subfolder_context_title, character_detection):
            filename_character_text = ""
        target_filename = target_filename_for(artist, filename_character_text, title, resolution, path.suffix)
        effective_folder = target_folder
        if target_folder == "Original Character" and getattr(config, "original_character_subfoldering", False) and artist:
            effective_folder = f"Original Character/{artist}"
        target_path = str(build_target_path(config.destination_root, effective_folder, target_filename))

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
        "variant_family": "",
        "variant_version": str(variant_metadata.get("version") or ""),
        "variant_descriptors": ", ".join(variant_metadata.get("descriptors") or []),
        "variant_credits": ", ".join(variant_metadata.get("credits") or []),
        "variant_decision": "",
        "variant_reason": "",
        "variant_rank": "",
        "status": status,
        "reason": ";".join(reasons),
        "notes": notes,
        "_variant_duration": get_video_duration(path, config.ffprobe_path),
        "_variant_frame_rate": get_video_frame_rate(path, config.ffprobe_path),
        "_variant_subfolder": normalize(" ".join(raw_context_parts)),
        "_variant_candidate_version": attached_version_candidate,
    }


def _split_variant_values(value: str) -> List[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _resolution_rank(label: str) -> int:
    norm = normalize(label)
    if "8k" in norm:
        return 600
    if "4k" in norm or "2160" in norm or "1440" in norm:
        return 500
    for token, rank in (("1080", 400), ("720", 300), ("480", 200)):
        if token in norm:
            return rank
    return 0


def _performance_signature(credits: Sequence[str]) -> str:
    canonical = sorted({str(item).strip() for item in credits if str(item).strip()}, key=normalize)
    return " + ".join(canonical)


def _preferred_performances(policy: Dict[str, Any], artist: str, character: str) -> List[str]:
    prefs = policy.get("preferred_performances") or {}
    candidates: Any = None
    artist_map = prefs.get("artists") or {}
    character_map = prefs.get("characters") or {}
    for key in (f"{artist}|{character}", character):
        for actual, value in character_map.items():
            if normalize(actual) == normalize(key):
                candidates = value
                break
        if candidates is not None:
            break
    if candidates is None:
        for actual, value in artist_map.items():
            if normalize(actual) == normalize(artist):
                candidates = value
                break
    if candidates is None:
        candidates = prefs.get("global") or []
    output: List[str] = []
    for entry in candidates or []:
        if isinstance(entry, str):
            values = re.split(r"\s*(?:\+|,|&|\band\b)\s*", entry, flags=re.I)
        elif isinstance(entry, (list, tuple)):
            values = [str(item) for item in entry]
        else:
            continue
        signature = _performance_signature(values)
        if signature and normalize(signature) not in {normalize(item) for item in output}:
            output.append(signature)
    return output


def _durations_equivalent(left: Dict[str, Any], right: Dict[str, Any], policy: Dict[str, Any]) -> bool:
    a = left.get("_variant_duration")
    b = right.get("_variant_duration")
    if a is None or b is None:
        left_stem = normalize(re.sub(r"\s*\(\d+\)\s*$", "", Path(left.get("original_name", "")).stem))
        right_stem = normalize(re.sub(r"\s*\(\d+\)\s*$", "", Path(right.get("original_name", "")).stem))
        return bool(left_stem and left_stem == right_stem)
    tolerance = max(
        float(policy.get("duration_tolerance_seconds", 0.5)),
        max(float(a), float(b)) * float(policy.get("duration_tolerance_percent", 2.0)) / 100.0,
    )
    return abs(float(a) - float(b)) <= tolerance


def _variant_core(row: Dict[str, Any], config: Config) -> str:
    metadata = {
        "version": row.get("variant_version", ""),
        "descriptors": _split_variant_values(row.get("variant_descriptors", "")),
        "credits": _split_variant_values(row.get("variant_credits", "")),
    }
    core = strip_variant_terms(row.get("clean_title", ""), metadata, config)
    core = re.sub(r"(?i)(?:^|[\s,;-])V\d+(?=$|[\s,;-])", " ", core)
    candidate = str(row.get("_variant_candidate_version") or "")
    if candidate:
        core = re.sub(r"(?i)(?:^|[\s,;-])" + re.escape(candidate[1:]) + r"(?=$|[\s,;-])", " ", core)
    return normalize(core)


def _variant_core_display(row: Dict[str, Any], config: Config) -> str:
    metadata = {
        "version": row.get("variant_version", ""),
        "descriptors": _split_variant_values(row.get("variant_descriptors", "")),
        "credits": _split_variant_values(row.get("variant_credits", "")),
    }
    core = strip_variant_terms(row.get("clean_title", ""), metadata, config)
    core = re.sub(r"(?i)(?:^|[\s,;-])V\d+(?=$|[\s,;-])", " ", core)
    return re.sub(r"\s+", " ", core).strip(" -_.,;")


def _refresh_variant_filename(row: Dict[str, Any], config: Config) -> None:
    metadata = {
        "version": row.get("variant_version", ""),
        "descriptors": _split_variant_values(row.get("variant_descriptors", "")),
        "credits": _split_variant_values(row.get("variant_credits", "")),
    }
    base = _variant_core_display(row, config)
    title = compose_variant_title(base, metadata)
    row["clean_title"] = title
    if row.get("target_folder") and row.get("resolution"):
        suffix = Path(row.get("source_path", "")).suffix or Path(row.get("original_name", "")).suffix or ".mp4"
        character_text = row.get("character", "")
        title_norm = normalize(title)
        embeds_character = any(contains_phrase(title_norm, normalize(part)) for part in character_parts(character_text))
        filename_character = "" if embeds_character else character_text
        filename = target_filename_for(row.get("artist", ""), filename_character, title, row.get("resolution", ""), suffix, preserve_variant_versions=True)
        row["target_filename"] = filename
        row["target_path"] = str(build_target_path(config.destination_root, row.get("target_folder", ""), filename))


def apply_variant_policy(rows: List[Dict[str, Any]], config: Config, enabled: Optional[bool] = None) -> Dict[str, int]:
    """Compare analyzed rows collection-wide and assign conservative retention decisions."""
    policy = getattr(config, "variant_policy", {}) or {}
    active = bool(policy.get("enabled", True)) if enabled is None else bool(enabled)
    stats = {"families": 0, "retained": 0, "review": 0, "superseded": 0}
    if not active:
        return stats

    groups: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        core = _variant_core(row, config)
        subfolder = normalize(row.get("_variant_subfolder", ""))
        key = (normalize(row.get("artist", "")), normalize(row.get("character", "")), core, subfolder)
        groups.setdefault(key, []).append(row)

    optional = {normalize(item) for item in ("POV", "Alt Angles", "Alt Angle", "Front Angle", "Loop", "Bonus", "Barelegs", "No Hat", "No X-Ray", "Facesit", "Pubes", "SFW", "NSFW")}
    appearance = {"std", "nude", "bra", "no bra"}
    max_performances = max(0, int(policy.get("max_preferred_performances", 2)))

    for key, family in groups.items():
        if len(family) < 2:
            row = family[0]
            descriptors = {normalize(item) for item in _split_variant_values(row.get("variant_descriptors", ""))}
            is_optional = bool(descriptors.intersection(optional))
            row["variant_decision"] = "variant_review" if is_optional else "retained"
            row["variant_reason"] = "standalone optional variant" if is_optional else "standalone scene"
            row["variant_rank"] = "1"
            _refresh_variant_filename(row, config)
            if is_optional and row.get("status") not in {"content_review", "silent"}:
                row["status"] = "variant_review"
                row["approved"] = "no"
                stats["review"] += 1
            else:
                stats["retained"] += 1
            continue

        family_id = hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:10]
        stats["families"] += 1
        family_max_resolution = max((_resolution_rank(row.get("resolution", "")) for row in family), default=0)
        for row in family:
            row["variant_family"] = family_id

        candidate_versions = {row.get("_variant_candidate_version") for row in family if row.get("_variant_candidate_version")}
        if candidate_versions and any(not row.get("_variant_candidate_version") and not row.get("variant_version") for row in family):
            for row in family:
                if not row.get("variant_version") and row.get("_variant_candidate_version"):
                    row["variant_version"] = row.get("_variant_candidate_version")
        explicit_versions = {row.get("variant_version") for row in family if row.get("variant_version")}
        if explicit_versions:
            for row in family:
                if not row.get("variant_version"):
                    row["variant_version"] = "V1"

        for version in {row.get("variant_version", "") for row in family}:
            subset = [row for row in family if row.get("variant_version", "") == version]
            has_nude = any("nude" in {normalize(x) for x in _split_variant_values(row.get("variant_descriptors", ""))} for row in subset)
            for row in subset:
                desc = _split_variant_values(row.get("variant_descriptors", ""))
                norms = {normalize(item) for item in desc}
                if (version or has_nude) and not norms.intersection(appearance) and not norms.intersection(optional):
                    desc.insert(0, "Std")
                    row["variant_descriptors"] = ", ".join(desc)

        preferred = _preferred_performances(policy, family[0].get("artist", ""), family[0].get("character", ""))
        preferred_norms = [normalize(item) for item in preferred[:max_performances]]
        exact: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
        for row in family:
            desc = _split_variant_values(row.get("variant_descriptors", ""))
            desc_without_nma = tuple(sorted(normalize(item) for item in desc if normalize(item) != "nma"))
            credits = _split_variant_values(row.get("variant_credits", ""))
            signature = (
                row.get("variant_version", ""), desc_without_nma,
                normalize(_performance_signature(credits)),
                "nma" in {normalize(item) for item in desc},
            )
            exact.setdefault(signature, []).append(row)

        for row in family:
            row["variant_decision"] = "retained"
            row["variant_reason"] = "selected policy winner"

        for signature, candidates in exact.items():
            ranked = sorted(candidates, key=lambda row: (
                _resolution_rank(row.get("resolution", "")),
                float(row.get("_variant_frame_rate") or 0),
                float(row.get("_variant_duration") or 0),
            ), reverse=True)
            winner = ranked[0]
            for loser in ranked[1:]:
                if _durations_equivalent(winner, loser, policy):
                    loser["variant_decision"] = "superseded_variant"
                    loser["variant_reason"] = f"equivalent lower-resolution encode; kept {winner.get('resolution', '')}"
                else:
                    loser["variant_decision"] = "variant_review"
                    loser["variant_reason"] = "distinct-duration lower-resolution variant"

        # NMA only supersedes regular audio for the same version, visuals, credits and duration.
        for row in family:
            if row.get("variant_decision") == "superseded_variant":
                continue
            desc = _split_variant_values(row.get("variant_descriptors", ""))
            norms = {normalize(item) for item in desc}
            if "nma" in norms:
                continue
            visual = tuple(sorted(norms))
            credit_sig = normalize(_performance_signature(_split_variant_values(row.get("variant_credits", ""))))
            for nma_row in family:
                nma_desc = {normalize(item) for item in _split_variant_values(nma_row.get("variant_descriptors", ""))}
                if "nma" not in nma_desc:
                    continue
                if row.get("variant_version", "") != nma_row.get("variant_version", ""):
                    continue
                if visual != tuple(sorted(nma_desc - {"nma"})):
                    continue
                if credit_sig != normalize(_performance_signature(_split_variant_values(nma_row.get("variant_credits", "")))):
                    continue
                if _durations_equivalent(row, nma_row, policy):
                    row["variant_decision"] = "superseded_variant"
                    row["variant_reason"] = "equivalent NMA variant preferred"
                    break

        for row in family:
            if row.get("variant_decision") == "superseded_variant":
                continue
            descriptors = {normalize(item) for item in _split_variant_values(row.get("variant_descriptors", ""))}
            if descriptors.intersection(optional):
                row["variant_decision"] = "variant_review"
                row["variant_reason"] = "optional visual/audio variant"
                continue
            if _resolution_rank(row.get("resolution", "")) < family_max_resolution:
                row["variant_decision"] = "variant_review"
                row["variant_reason"] = "unique lower-resolution variant"
                continue
            credit_sig = normalize(_performance_signature(_split_variant_values(row.get("variant_credits", ""))))
            if credit_sig and (not preferred_norms or credit_sig not in preferred_norms):
                row["variant_decision"] = "variant_review"
                row["variant_reason"] = "unranked or excess performance signature"

        ordered = sorted(family, key=lambda row: (
            0 if row.get("variant_decision") == "retained" else 1 if row.get("variant_decision") == "variant_review" else 2,
            -_resolution_rank(row.get("resolution", "")),
            row.get("original_name", "").lower(),
        ))
        for rank, row in enumerate(ordered, 1):
            row["variant_rank"] = str(rank)
            _refresh_variant_filename(row, config)
            if row.get("status") in {"content_review", "silent"}:
                row["variant_reason"] = f"{row.get('status')} takes precedence; " + row.get("variant_reason", "")
                continue
            if row.get("variant_decision") == "superseded_variant":
                row["status"] = "superseded_variant"
                row["approved"] = "no"
                stats["superseded"] += 1
            elif row.get("variant_decision") == "variant_review":
                row["status"] = "variant_review"
                row["approved"] = "no"
                stats["review"] += 1
            else:
                stats["retained"] += 1

    return stats


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
    angle_quarantine_report: Optional[Dict[str, Any]] = None,
    variant_stats: Optional[Dict[str, int]] = None,
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
        f"- Variant families: {(variant_stats or {}).get('families', 0)}",
        f"- Variant winners retained: {(variant_stats or {}).get('retained', 0)}",
        f"- Variants requiring review: {(variant_stats or {}).get('review', 0)}",
        f"- Superseded variants held on apply: {(variant_stats or {}).get('superseded', 0)}",
        f"- Reference filename samples: {reference.naming_style.sample_count}",
        f"- Output resolution labels: {naming_style_resolution_summary(reference)}",
        "",
        "## By Destination",
        "",
    ]
    for folder, count in sorted(by_folder.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {folder}: {count}")

    # Angle pack quarantine / suggestion section (Priority 1 repair: surface suggestions even with 0 moves performed)
    if angle_quarantine_report and (angle_quarantine_report.get("quarantined_count", 0) > 0 or angle_quarantine_report.get("confirmed") or angle_quarantine_report.get("rejected")):
        lines.extend(["", "## Angle Pack Quarantine", ""])
        qcount = angle_quarantine_report.get("quarantined_count", 0)
        if qcount > 0:
            lines.append(f"- Individual camera variants moved: {qcount}")
        else:
            lines.append("- Individual camera variants: 0 moved (suggestions only; preview is read-only)")
        confirmed = angle_quarantine_report.get("confirmed", [])
        if confirmed:
            strong = sum(1 for c in confirmed if c.get("confidence") == "strong")
            label = "Suggested compilations (not performed)" if qcount == 0 else "Confirmed compilations"
            lines.append(f"- {label}: {len(confirmed)} ({strong} strong confidence)")
            for c in confirmed[:10]:
                dur = f" (dur×{c.get('duration_ratio')})" if c.get('duration_ratio') else ""
                cams_note = ""
                if qcount == 0 and c.get("cam_files"):
                    cams_note = f" (example cams: {', '.join(c['cam_files'][:3])}...)"
                lines.append(f"  - {c['base']}... — {c['num_cams']} cams, {c['confidence']}{dur}{cams_note}")
        rejected = angle_quarantine_report.get("rejected", [])
        if rejected:
            lines.append(f"- All Angles with no matching individual cams found: {len(rejected)}")
            for b in rejected[:8]:
                lines.append(f"  - {b}...")

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
    files = discover_videos(source, config, show_progress=True)

    # Angle pack detection (read-only in normal preview).
    # --quarantine-angle-variants now means "detect + report suggestions in CSV/summary".
    # Actual moves never happen from the preview command (perform_quarantine=False).
    # Affected files will be present in the CSV with status="angle_variant_review" etc.
    angle_quarantine_report: Dict[str, Any] = {"quarantined_count": 0, "confirmed": [], "rejected": []}
    if getattr(args, "quarantine_angle_variants", False):
        # Force read-only even if caller passed the flag; preview is never destructive.
        files, angle_quarantine_report = quarantine_angle_variants(files, source, config, perform_quarantine=False)

    # Progress indicator for analysis phase (the longest part of preview)
    rows = []
    total = len(files)
    if total == 0:
        print("No video files found to analyze.")
    else:
        print(f"Analyzing {total} video files...")
        last_percent = -1
        for i, path in enumerate(files, 1):
            row = analyze_file(path, source, config, reference)
            rows.append(row)

            percent = int((i / total) * 100)
            # Update at most every 1% or every 10 files to keep output reasonable
            if percent != last_percent or i % 10 == 0 or i == total:
                bar_len = 25
                filled = int(bar_len * i // total)
                bar = '#' * filled + '-' * (bar_len - filled)
                print(f"\r  [{bar}] {percent:3d}% ({i}/{total})", end="", flush=True)
                last_percent = percent
        print()  # Finish the progress line

    variant_enabled = not getattr(args, "no_variant_analysis", False)
    variant_stats = apply_variant_policy(rows, config, enabled=variant_enabled)

    # Post-process angle variant suggestions (Priority 1 repair) via helper.
    # Ensures explicit visibility in CSV (status/notes) without omitting files.
    mark_angle_variants_for_review(rows, angle_quarantine_report)

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

    # Deduplicate target filenames so that no two files in this batch get the exact same name.
    # This commonly happens when title cleaning is very aggressive on collector dumps.
    deduplicate_target_filenames(rows)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else source
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path, md_path = unique_plan_paths(output_dir, run_id())
    write_csv(csv_path, rows)
    write_preview_summary(md_path, source, config.destination_root, rows, reference, angle_quarantine_report, variant_stats)

    print(f"Preview complete: {len(rows)} video(s)")
    print(f"Reference filename samples: {reference.naming_style.sample_count}")
    print(f"Output resolution labels: {naming_style_resolution_summary(reference)}")
    print(f"Variant analysis: {variant_stats['families']} families, {variant_stats['retained']} retained, {variant_stats['review']} review, {variant_stats['superseded']} superseded")
    print(f"CSV plan: {csv_path}")
    print(f"Summary:  {md_path}")
    print("Edit the CSV approved/status/target columns, then run apply.")
    return 0


def quarantine_angle_variants(
    files: List[Path], source: Path, config: Config, *, perform_quarantine: bool = False
) -> Tuple[List[Path], Dict[str, Any]]:
    """Detect camera angle packs with *high confidence*.

    Preview behavior (perform_quarantine=False, the safe default):
      - Detects matching All-Angles + individual Cam packs.
      - Populates report["confirmed"] / rejected.
      - **Never moves, renames, quarantines or deletes files.**
      - The caller (preview) will ensure affected files appear in the CSV with
        explicit status="angle_variant_review", approved="no" and notes so they
        are visible for review (never silently omitted).

    When perform_quarantine=True (only for explicit/opt-in use outside normal preview):
      - Actually moves the individual cam files into the variants subfolder.
      - Updates moved_count and prints "Quarantined...".

    Method (detection always runs):
      For each "All Angles" file, extract its precise scene base (text immediately
      before "All Angles"). Then scan the source for real individual cam files
      whose names begin with that exact base + " Cam ".

      If such matching cam files are found -> (optionally) quarantine them (with optional
      duration cross-check for extra confidence).
      If no matching individual cams exist for that base -> record as rejected.

    Returns:
        (remaining_files, report_dict)

    The report contains:
      - quarantined_count (actual moves performed; 0 when !perform_quarantine)
      - confirmed: list of dicts with base, num_cams, confidence, duration_ratio, and cam_files (names)
      - rejected: list of bases that had an All Angles but no matching cams
      - (when !perform) the confirmed entries still list what *would* be quarantined
    """
    if not files:
        return files, {"quarantined_count": 0, "confirmed": [], "rejected": []}

    variants_folder_name = getattr(config, "angle_variants_folder_name", "_r34_angle_variants")
    variants_dir = source / variants_folder_name
    ffprobe_path = getattr(config, "ffprobe_path", "ffprobe")

    # Find all potential "All Angles" compilations
    angle_compilations = [
        f for f in files
        if re.search(r'(?i)\b(all\s*angles?|allangles?|all_angle)\b', f.name)
    ]

    if not angle_compilations:
        return files, {"quarantined_count": 0, "confirmed": [], "rejected": []}

    moved_count = 0
    confirmed: List[Dict[str, Any]] = []
    rejected: List[str] = []

    for comp in angle_compilations:
        stem = comp.stem
        match = re.search(r'(?i)^(.+?)\s*(?:all\s*angles?|allangles?|all_angle)\b', stem)
        if not match:
            continue

        base = match.group(1).strip()
        if len(base) < 8:
            continue

        base_pattern = re.escape(base)
        cam_pattern = re.compile(
            rf'(?i)^{base_pattern}\s*cam\s*\d+',
            re.IGNORECASE
        )

        individual_cams: List[Path] = []
        for f in source.rglob('*.mp4'):
            if variants_dir in f.parents:
                continue
            if cam_pattern.search(f.stem):
                individual_cams.append(f)

        if not individual_cams:
            rejected.append(base)
            continue

        # Optional duration cross-check for confidence
        all_dur = get_video_duration(comp, ffprobe_path)
        cam_durs = [d for d in (get_video_duration(c, ffprobe_path) for c in individual_cams) if d]
        duration_ratio = None
        confidence = "medium"

        if all_dur and cam_durs:
            max_cam = max(cam_durs)
            duration_ratio = all_dur / max_cam if max_cam > 0 else 0
            # Strong if the compilation is substantially longer than any single cam
            if duration_ratio > 1.6:
                confidence = "strong"

        # If we have 2+ cams, upgrade to strong even without duration data
        if len(individual_cams) >= 2 and confidence == "medium":
            confidence = "strong"

        confirmed.append({
            "base": base,
            "num_cams": len(individual_cams),
            "confidence": confidence,
            "duration_ratio": round(duration_ratio, 2) if duration_ratio else None,
            "cam_files": [p.name for p in individual_cams],
        })

        if perform_quarantine:
            variants_dir.mkdir(parents=True, exist_ok=True)

            for cam_file in individual_cams:
                dest = variants_dir / cam_file.name
                try:
                    if not dest.exists():
                        _safe_shutil_move(cam_file, dest)
                        print(f"  Quarantined camera variant -> {variants_folder_name}/ : {cam_file.name}")
                        moved_count += 1
                    else:
                        print(f"  (already quarantined) {cam_file.name}")
                except Exception as ex:
                    print(f"  Warning: failed to move {cam_file.name}: {ex}")
        # else: dry-run / preview mode — report only, no FS mutation

    if perform_quarantine and (moved_count or rejected):
        print(f"  -> Angle pack quarantine: {moved_count} individual files moved to {variants_folder_name}/")
        if confirmed:
            strong = sum(1 for c in confirmed if c["confidence"] == "strong")
            print(f"     Confirmed compilations: {len(confirmed)} ({strong} strong, {len(confirmed)-strong} medium)")
            for c in confirmed[:6]:
                dur_info = f", dur×{c['duration_ratio']}" if c['duration_ratio'] else ""
                print(f"       • {c['base']}... ({c['num_cams']} cams, {c['confidence']}{dur_info})")
        if rejected:
            print(f"     Rejected (All Angles with no matching cams found): {len(rejected)}")
    elif not perform_quarantine and confirmed:
        print(f"  -> Angle pack suggestions (NOT performed — preview is read-only): {len(confirmed)} compilation(s)")
        strong = sum(1 for c in confirmed if c.get("confidence") == "strong")
        print(f"     Suggested: {len(confirmed)} ({strong} strong, {len(confirmed)-strong} medium)")
        for c in confirmed[:6]:
            dur_info = f", dur×{c.get('duration_ratio')}" if c.get('duration_ratio') else ""
            cams = ", ".join(c.get("cam_files", [])[:3])
            print(f"       • {c['base']}... ({c['num_cams']} cams: {cams}..., {c['confidence']}{dur_info})")
        if rejected:
            print(f"     Rejected (All Angles with no matching cams found): {len(rejected)}")

    # Always return the caller's file list (do not filter here in dry/preview mode).
    # The caller will ensure any suggested angle variants are visible in the CSV
    # via explicit status/notes/suggested fields (never silently omitted).
    remaining = list(files)
    report = {
        "quarantined_count": moved_count,
        "confirmed": confirmed,
        "rejected": rejected,
    }
    return sorted(remaining, key=lambda p: str(p).lower()), report


def mark_angle_variants_for_review(rows: list, angle_report: Dict[str, Any]) -> None:
    """Post-process rows from preview so that detected (but not moved) angle variants
    are explicitly visible in the CSV plan with status + notes (never silently omitted).
    Mutates the row dicts in place. Called from command_preview after analyze.
    """
    suggested = set()
    for c in (angle_report or {}).get("confirmed", []):
        for nm in c.get("cam_files", []):
            suggested.add(nm)
    for row in rows:
        if row.get("original_name") in suggested:
            row["status"] = "variant_review"
            row["approved"] = "no"
            row["variant_decision"] = "variant_review"
            row["variant_reason"] = "individual angle variant"
            note = "Individual angle variant requires review; not moved during preview."
            existing = (row.get("notes") or "").strip()
            row["notes"] = f"{existing}; {note}".strip("; ").strip() if existing else note
            rsn = (row.get("reason") or "").strip()
            if "angle_variant" not in rsn:
                row["reason"] = (rsn + ";angle_variant_suggested").strip(";")


def replace_config(config: Config, **updates: object) -> Config:
    data = {
        "destination_root": config.destination_root,
        "video_extensions": config.video_extensions,
        "ffprobe_path": config.ffprobe_path,
        "review_folder_name": config.review_folder_name,
        "content_review_folder_name": config.content_review_folder_name,
        "silent_animations_folder_name": getattr(config, "silent_animations_folder_name", "_r34_silent"),
        "confidence_threshold": config.confidence_threshold,
        "allow_create_destination_folders": config.allow_create_destination_folders,
        "artist_aliases": config.artist_aliases,
        "folder_aliases": config.folder_aliases,
        "character_mappings": config.character_mappings,
        "canonical_character_aliases": config.canonical_character_aliases,
        "title_token_replacements": config.title_token_replacements,
        "filename_overrides": getattr(config, "filename_overrides", {}),
        "content_review_terms": config.content_review_terms,
        "junk_tokens": config.junk_tokens,
        "preserve_tokens": config.preserve_tokens,
        "audio_credits": getattr(config, "audio_credits", ()),
        "known_collectors": getattr(config, "known_collectors", ()),
        "collection_folder_indicators": getattr(config, "collection_folder_indicators", ()),
        "use_ai_for_unknown_characters": getattr(config, "use_ai_for_unknown_characters", False),
        "ai_model": getattr(config, "ai_model", "grok-3"),
        "ai_api_key_env_var": getattr(config, "ai_api_key_env_var", "XAI_API_KEY"),
        "auto_load_xai_key": getattr(config, "auto_load_xai_key", True),
        "original_character_subfoldering": getattr(config, "original_character_subfoldering", False),
        "learned_franchises_file": getattr(config, "learned_franchises_file", "learned_character_franchises.json"),
        "extract_embedded_titles": getattr(config, "extract_embedded_titles", False),
        "angle_variants_folder_name": getattr(config, "angle_variants_folder_name", "_r34_angle_variants"),
        "variant_policy": getattr(config, "variant_policy", DEFAULT_VARIANT_POLICY),
    }
    data.update(updates)
    new_cfg = Config(**data)
    # Preserve _loaded_config_path (set via object.__setattr__ in load_config for frozen dataclass;
    # used by load_learned_franchises etc. for relative paths when GUI uses --config).
    # See test_learned_resolves_relative_to_loaded_config_path.
    if hasattr(config, "_loaded_config_path"):
        object.__setattr__(new_cfg, "_loaded_config_path", getattr(config, "_loaded_config_path", None))
    return new_cfg


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


def path_is_inside(path: Path, root: Path) -> bool:
    """Return True when path resolves inside root, without requiring the file to exist."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def clean_relative_folder_parts(folder: str) -> List[str]:
    """Split a CSV/UI folder value into clean relative path components."""
    parts = re.split(r"[\\/]+", str(folder or ""))
    return [part.strip(" .\t\r\n") for part in parts if part.strip(" .\t\r\n")]


def build_target_path(destination_root: Path, target_folder: str, target_filename: str) -> Path:
    target = Path(destination_root)
    for part in clean_relative_folder_parts(target_folder):
        target = target / part
    return target / target_filename


def apply_row(
    row: Dict[str, str],
    source_root: Path,
    run: str,
    review_folder_name: str,
    quarantine_unapproved: bool,
    content_review_folder_name: str = "_r34_content_review",
    silent_animations_folder_name: str = "_r34_silent",
    angle_variants_folder_name: str = "_r34_angle_variants",
    review_items_folder_name: str = None,  # for "review" status outliers
    destination_root: Path | None = None,
    superseded_folder_name: str = "_r34_superseded_variants",
) -> Dict[str, str]:
    result = dict(row)
    source = Path(row.get("source_path", ""))
    target_text = row.get("target_path", "")
    status_raw = str(row.get("status", "")).strip().lower()
    status = normalize(row.get("status", ""))
    approved = approved_value(row.get("approved", ""))

    result["apply_result"] = ""
    result["apply_message"] = ""
    result["original_path"] = str(source)
    result["held_path"] = ""

    if status_raw == "content_review" or status == "content review":
        if not source.exists():
            result["apply_result"] = "missing_source"
            result["apply_message"] = str(source)
            return result
        dest = content_review_path(source, source_root, content_review_folder_name, run)
        _safe_shutil_move(source, dest)
        result["apply_result"] = "held_content_review"
        result["apply_message"] = str(dest)
        return result

    if status_raw == "superseded_variant" or status == "superseded variant":
        if not source.exists():
            result["apply_result"] = "missing_source"
            result["apply_message"] = str(source)
            return result
        dest = content_review_path(source, source_root, superseded_folder_name, run)
        _safe_shutil_move(source, dest)
        result["apply_result"] = "held_superseded_variant"
        result["apply_message"] = str(dest)
        result["held_path"] = str(dest)
        return result

    if not approved:
        if quarantine_unapproved and source.exists():
            dest = quarantine_path(source, source_root, review_folder_name, run)
            _safe_shutil_move(source, dest)
            result["apply_result"] = "quarantined_unapproved"
            result["apply_message"] = str(dest)
        else:
            result["apply_result"] = "skipped_unapproved"
            result["apply_message"] = "approved is not true/yes"
        return result

    if status_raw == "silent" or status == "silent":
        if not source.exists():
            result["apply_result"] = "missing_source"
            result["apply_message"] = str(source)
            return result
        dest = content_review_path(source, source_root, silent_animations_folder_name, run)
        _safe_shutil_move(source, dest)
        result["apply_result"] = "held_silent"
        result["apply_message"] = str(dest)
        result["held_path"] = str(dest)
        return result

    if status_raw == "review" or status == "review":
        if not source.exists():
            result["apply_result"] = "missing_source"
            result["apply_message"] = str(source)
            return result
        folder = review_items_folder_name or review_folder_name
        dest = content_review_path(source, source_root, folder, run)
        _safe_shutil_move(source, dest)
        result["apply_result"] = "held_for_review"
        result["apply_message"] = str(dest)
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

    if destination_root is not None and row.get("target_folder") and row.get("target_filename"):
        target = build_target_path(Path(destination_root), row.get("target_folder", ""), row.get("target_filename", ""))
    else:
        target = Path(target_text)
    if destination_root is not None and not path_is_inside(target, Path(destination_root)):
        result["apply_result"] = "invalid_target"
        result["apply_message"] = f"target_path is outside destination_root: {target}"
        return result

    if target.exists():
        dest = quarantine_path(source, source_root, review_folder_name, run)
        _safe_shutil_move(source, dest)
        result["apply_result"] = "quarantined_duplicate" if likely_same_clip(dest, target) else "quarantined_conflict"
        result["apply_message"] = f"{dest} (target exists: {target})"
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    _safe_shutil_move(source, target)
    result["apply_result"] = "moved"
    result["apply_message"] = str(target)
    return result


def _safe_shutil_move(src: str | Path, dst: str | Path, max_retries: int = 8, base_delay: float = 0.25) -> None:
    """Robust move for Windows file locks (common with video files in Explorer, players, AV).

    Retries on PermissionError/WinError 32 (file in use) with exponential backoff.
    Other errors are raised immediately.
    """
    src_s = str(src)
    dst_s = str(dst)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            shutil.move(src_s, dst_s)
            return
        except (PermissionError, OSError) as e:
            last_err = e
            msg = str(e).lower()
            winerr = getattr(e, "winerror", None)
            if winerr == 32 or "being used by another process" in msg or "access is denied" in msg:
                delay = min(base_delay * (2 ** attempt), 8.0)
                name = Path(src_s).name
                print(f"  [move retry {attempt+1}/{max_retries}] File in use, waiting {delay:.1f}s: {name}")
                time.sleep(delay)
                continue
            # non-retryable
            raise
    if last_err:
        raise last_err


def write_apply_log(plan: Path, rows: Sequence[Dict[str, str]], run: str) -> Path:
    log_path = plan.with_name(f"r34_apply_{run}.csv")
    # Include learning snapshot columns for reversible apply-driven learning.
    # Old apply logs without them remain readable for undo (read_csv sets defaults to "").
    fieldnames = CSV_COLUMNS + [
        "apply_result",
        "apply_message",
        "original_path",
        "held_path",
        "learned_character",
        "learned_franchise",
        "pre_learned_franchise",
    ]
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
    if row.get("apply_result") == "error":
        msg = row.get("apply_message", "")
        return f"{original} -> ERROR ({msg})" if msg else f"{original} -> ERROR"
    target_name = row.get("target_filename") or Path(row.get("target_path", "")).name
    if target_name and target_name != original:
        # Only show the actual filename that will appear in Explorer.
        # The folder is visible in the CSV if the user needs it.
        return f"{original} -> {target_name}"
    return original


def command_apply(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plan = Path(args.plan).resolve()
    rows = read_csv(plan)
    source_root = Path(args.source_root).resolve() if args.source_root else infer_source_root(rows)
    run = plan_run_id(plan)

    # Snapshot pre-apply learned state for any learnable (char, target_folder) in approved rows.
    # This enables exact reversal on undo even if the same char appears in multiple rows.
    current_learned = load_learned_franchises(config)
    pre_snapshots: Dict[str, str] = {}
    to_commit: Dict[str, str] = {}
    for row in rows:
        if approved_value(row.get("approved", "")):
            char = (row.get("character") or "").strip()
            folder = (row.get("target_folder") or "").strip()
            if _is_learnable_franchise(char, folder):
                n = normalize(char)
                if n not in pre_snapshots:
                    pre_snapshots[n] = current_learned.get(n, "")
                to_commit[n] = folder  # last approved value in the plan wins for the batch

    applied: List[Dict[str, str]] = []
    total = len(rows)
    if total == 0:
        print("Apply progress: 0/0 (100%) - no rows to process", flush=True)
    for index, row in enumerate(rows, start=1):
        try:
            result = apply_row(
                row,
                source_root,
                run,
                config.review_folder_name,
                args.quarantine_unapproved,
                config.content_review_folder_name,
                getattr(config, "silent_animations_folder_name", "_r34_silent"),
                review_items_folder_name=config.review_folder_name,
                destination_root=config.destination_root,
                superseded_folder_name=(getattr(config, "variant_policy", {}) or {}).get("superseded_folder_name", "_r34_superseded_variants"),
            )
        except Exception as ex:
            # Never let one locked/missing file kill the entire apply batch.
            # Record the failure so the log captures partial success.
            orig_name = row.get("original_name") or Path(row.get("source_path", "")).name
            print(f"  ERROR applying row for {orig_name}: {ex}", flush=True)
            result = dict(row)
            result["apply_result"] = "error"
            result["apply_message"] = str(ex)
        # Attach reversible learning metadata to EVERY row (populated for those that qualified).
        # Only rows with apply_result=="moved" will actually cause a write to the learned file.
        char = (row.get("character") or "").strip()
        folder = (row.get("target_folder") or "").strip()
        n = normalize(char) if char else ""
        if _is_learnable_franchise(char, folder) and n in pre_snapshots:
            result["learned_character"] = char
            result["learned_franchise"] = folder
            result["pre_learned_franchise"] = pre_snapshots[n]
        else:
            result["learned_character"] = ""
            result["learned_franchise"] = ""
            result["pre_learned_franchise"] = ""
        applied.append(result)
        percent = round((index / total) * 100) if total else 100
        outcome = result.get("apply_result", "unknown")
        label = apply_progress_label(result)
        print(f"Apply progress: {index}/{total} ({percent}%) - {outcome}: {label}", flush=True)

    # Commit learning ONLY for rows that actually moved (satisfactory human-approved result executed).
    # This tells the script "these classifications were good; use them to judge future ops."
    final_commits: Dict[str, str] = {}
    for res in applied:
        if res.get("apply_result") == "moved":
            lc = res.get("learned_character", "").strip()
            lf = res.get("learned_franchise", "").strip()
            if _is_learnable_franchise(lc, lf):
                final_commits[normalize(lc)] = lf
    learned_log_path = Path()
    if final_commits:
        learned_log_path = write_learned_franchises(final_commits, config)
        print(f"Learned {len(final_commits)} character->franchise mapping(s) from satisfactory apply: {learned_log_path}")

    log_path = write_apply_log(plan, applied, run)
    counts: Dict[str, int] = {}
    for row in applied:
        key = row.get("apply_result", "unknown")
        counts[key] = counts.get(key, 0) + 1
    print("Apply complete:")
    for key, count in sorted(counts.items()):
        print(f"  {key}: {count}")
    print(f"Apply log: {log_path}")
    if learned_log_path:
        print(f"  (learning committed to {learned_log_path.name})")
    return 0


def undo_row(
    row: Dict[str, str],
    source_root: Path,
    run: str,
    review_folder_name: str,
    config: Optional[Config] = None,
) -> Dict[str, str]:
    """Attempt to reverse one applied row (file move + any learning committed by that apply)."""
    result = dict(row)
    result["undo_result"] = ""
    result["undo_message"] = ""
    result["learning_reverted"] = ""

    original_source = Path(row.get("source_path", ""))
    apply_result = row.get("apply_result", "")
    apply_message = row.get("apply_message", "")

    reversible_results = {"moved", "held_superseded_variant"}
    if apply_result not in reversible_results:
        result["undo_result"] = "skipped_non_move"
        result["undo_message"] = f"apply_result was {apply_result}"
        return result

    # The file is currently at the target location recorded in apply_message
    current_location = Path(row.get("held_path") or apply_message) if apply_message or row.get("held_path") else Path(row.get("target_path", ""))

    if not current_location.exists():
        result["undo_result"] = "missing_at_target"
        result["undo_message"] = str(current_location)
        return result

    # Destination for undo = original source location (with original name)
    original_name = row.get("original_name", current_location.name)
    restore_path = original_source if str(original_source).endswith(original_name) else (original_source.parent / original_name)

    if restore_path.exists():
        # Conflict at original location — quarantine the file we're trying to restore
        quarantine_dest = quarantine_path(current_location, source_root, review_folder_name, f"undo_{run}")
        _safe_shutil_move(current_location, quarantine_dest)
        result["undo_result"] = "quarantined_conflict_on_undo"
        result["undo_message"] = str(quarantine_dest)
        # Still attempt learning revert: the apply had committed it as part of the original move
        _maybe_revert_learning(row, config, result)
        return result

    # Safe to restore
    restore_path.parent.mkdir(parents=True, exist_ok=True)
    _safe_shutil_move(current_location, restore_path)
    result["undo_result"] = "restored"
    result["undo_message"] = str(restore_path)
    _maybe_revert_learning(row, config, result)
    return result


def _maybe_revert_learning(row: Dict[str, str], config: Optional[Config], result: Dict[str, str]) -> None:
    """Internal: if row has learning snapshot and config, attempt safe revert."""
    if not config:
        return
    learned_char = (row.get("learned_character") or "").strip()
    learned_f = (row.get("learned_franchise") or "").strip()
    pre_f = row.get("pre_learned_franchise", "") or ""
    n = normalize(learned_char) if learned_char else ""
    if n and learned_f and _is_learnable_franchise(learned_char, learned_f):
        if revert_learned_franchise(n, learned_f, pre_f, config):
            result["learning_reverted"] = f"{learned_char}->{pre_f or '(removed)'}"
        else:
            result["learning_reverted"] = "no_change_or_stale"


def write_undo_log(plan: Path, rows: Sequence[Dict[str, str]], run: str) -> Path:
    log_path = plan.with_name(f"r34_undo_{run}.csv")
    fieldnames = list(rows[0].keys()) if rows else CSV_COLUMNS + ["undo_result", "undo_message"]
    with log_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return log_path


def command_undo(args: argparse.Namespace) -> int:
    log_path = Path(args.log).resolve()
    if not log_path.exists():
        raise SystemExit(f"Undo log not found: {log_path}")

    applied_rows = read_csv(log_path)

    # Try to infer source root from the log
    source_root = Path(args.source_root).resolve() if args.source_root else infer_source_root(applied_rows)

    run = plan_run_id(log_path)  # reuse the function, it will extract the timestamp

    # Load config so we can revert any learning that was committed during the original apply
    config = load_config(args.config)

    undone: List[Dict[str, str]] = []
    total = len(applied_rows)

    print(f"Undoing apply from log: {log_path}")
    print(f"Source root for restore: {source_root}")

    for index, row in enumerate(applied_rows, start=1):
        try:
            result = undo_row(row, source_root, run, "_r34_review", config)
        except Exception as ex:
            orig_name = row.get("original_name") or Path(row.get("source_path", "")).name
            print(f"  ERROR undoing row for {orig_name}: {ex}", flush=True)
            result = dict(row)
            result["undo_result"] = "error"
            result["undo_message"] = str(ex)
            result["learning_reverted"] = ""
        undone.append(result)

        percent = round((index / total) * 100) if total else 100
        outcome = result.get("undo_result", "unknown")
        msg = result.get("undo_message", "")
        learn = result.get("learning_reverted", "")
        extra = f" | learning: {learn}" if learn else ""
        print(f"Undo progress: {index}/{total} ({percent}%) - {outcome}: {msg}{extra}", flush=True)

    undo_log = write_undo_log(log_path, undone, run)

    counts: Dict[str, int] = {}
    for row in undone:
        key = row.get("undo_result", "unknown")
        counts[key] = counts.get(key, 0) + 1

    learning_reverts = sum(1 for r in undone if r.get("learning_reverted") and "no_change" not in r.get("learning_reverted", ""))
    print("\nUndo complete:")
    for key, count in sorted(counts.items()):
        print(f"  {key}: {count}")
    if learning_reverts:
        print(f"  learning_reverted: {learning_reverts}")
    print(f"Undo log: {undo_log}")

    return 0


def command_eval_namegen(args: argparse.Namespace) -> int:
    """P5 foundation: run name-gen on cases JSON.

    - Creates temp dummy files only (no user videos required).
    - Forces use_ai=False and patches the real helpers (probe_resolution, has_audio_stream, get_video_duration).
    - Prints artist/character/target_folder/filename/status accuracy.
    - Never calls Grok.
    """
    import json as _json
    from unittest.mock import patch as _patch

    cases_p = Path(args.cases)
    if not cases_p.exists():
        print(f"ERROR: cases file not found: {cases_p}")
        return 1
    cases = _json.loads(cases_p.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        print("No cases or invalid format.")
        return 0

    total = len(cases)
    print(f"Eval namegen on {total} cases (synthetic, no Grok, no real videos)...")

    a_ok = c_ok = f_ok = fn_ok = s_ok = 0

    # basic mappings so common cases have a chance (copied subset from test defaults; no new heuristics)
    basic_char_map = {
        "2b": "Nier Automata", "2p": "Nier Automata", "a2": "Nier Automata",
        "d va": "Overwatch", "dva": "Overwatch",
        "botw zelda": "Legend of Zelda", "palutena": "Kid Icarus",
        "peach": "Super Mario", "tifa": "Final Fantasy",
        "chun li": "Street Fighter", "chun-li": "Street Fighter",
    }
    basic_canon = {k: v for k, v in basic_char_map.items()}  # simplistic
    basic_artist = {"pantsushi": "Pantsushi", "nodu": "Nodu", "bewyx": "Bewyx"}

    for case in cases:
        orig = case.get("original_name") or "case.mp4"
        parent = case.get("source_parent") or "src"
        exp = case.get("expected") or {}

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / parent
            src.mkdir(parents=True, exist_ok=True)
            dummy = src / orig
            dummy.write_bytes(b"fake video")

            dest = Path(td) / "dest"
            dest.mkdir(exist_ok=True)

            # temp config json (ai off, basic maps for the cases)
            tcfg = {
                "destination_root": str(dest),
                "video_extensions": [".mp4"],
                "ffprobe_path": "ffprobe",
                "review_folder_name": "_r34_review",
                "content_review_folder_name": "_r34_content_review",
                "silent_animations_folder_name": "_r34_silent",
                "confidence_threshold": 0.9,
                "allow_create_destination_folders": False,
                "artist_aliases": basic_artist,
                "folder_aliases": {},
                "character_mappings": basic_char_map,
                "canonical_character_aliases": basic_canon,
                "title_token_replacements": {},
                "content_review_terms": {},
                "junk_tokens": ["1080p", "4k", "unwatermarked"],
                "preserve_tokens": ["2B", "2P", "D.Va"],
                "audio_credits": ["audiodude", "evilaudio", "multiaudio"],
                "known_collectors": ["audio collection"],
                "collection_folder_indicators": ["collection"],
                "use_ai_for_unknown_characters": False,
                "ai_model": "grok-3",
                "ai_api_key_env_var": "XAI_API_KEY",
                "auto_load_xai_key": False,
                "original_character_subfoldering": False,
                "learned_franchises_file": "learned.json",
                "extract_embedded_titles": False,
                "angle_variants_folder_name": "_r34_angle_variants",
            }
            tcfg_p = Path(td) / "tcfg.json"
            tcfg_p.write_text(_json.dumps(tcfg), encoding="utf-8")
            cfg = load_config(tcfg_p)
            ref = build_reference_data(dest, cfg)

            with _patch("r34_organizer.probe_resolution", return_value=("1080p", "", "")), \
                 _patch("r34_organizer.has_audio_stream", return_value=True), \
                 _patch("r34_organizer.get_video_duration", return_value=120.0), \
                 _patch("r34_organizer.query_grok_for_character_franchise", return_value=None):
                row = analyze_file(dummy, src, cfg, ref)

            if (row.get("artist") or "") == (exp.get("artist") or ""):
                a_ok += 1
            if (row.get("character") or "") == (exp.get("character") or ""):
                c_ok += 1
            if (row.get("target_folder") or "") == (exp.get("target_folder") or ""):
                f_ok += 1
            # filename: loose check (contains or matches if provided)
            tgt = row.get("target_filename") or ""
            exp_fn = exp.get("target_filename") or ""
            if exp_fn:
                if exp_fn in tgt or tgt in exp_fn:
                    fn_ok += 1
            else:
                fn_ok += 1  # if no expectation, count as pass for foundation
            if (row.get("status") or "") == (exp.get("status") or ""):
                s_ok += 1

    print("Accuracy (artist/character/folder/filename/status):")
    def pct(ok): return f"{ok}/{total} ({100*ok/total:.1f}%)" if total else "0/0"
    print(f"  artist: {pct(a_ok)}")
    print(f"  character: {pct(c_ok)}")
    print(f"  folder: {pct(f_ok)}")
    print(f"  filename: {pct(fn_ok)}")
    print(f"  status: {pct(s_ok)}")
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
    preview.add_argument(
        "--no-variant-analysis",
        action="store_true",
        help="Disable collection-level variant naming and retention analysis for this preview.",
    )
    preview.add_argument(
        "--quarantine-angle-variants",
        action="store_true",
        default=False,
        help="Detect individual camera-angle variants when an All Angles compilation exists and route them to variant review. This does not move files during preview.",
    )
    preview.add_argument(
        "--no-quarantine-angle-variants",
        dest="quarantine_angle_variants",
        action="store_false",
        help="Disable angle-variant detection/reporting in the preview (default behavior).",
    )
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

    undo = sub.add_parser("undo", help="Undo a previous apply using its log file.")
    undo.add_argument("--log", required=True, help="Path to an r34_apply_*.csv log file from a previous apply.")
    undo.add_argument("--source-root", help="Override source root for restoring files (usually not needed).")
    undo.set_defaults(func=command_undo)

    evalp = sub.add_parser("eval-namegen", help="Evaluate current name-generation logic on synthetic cases (no real video files, no Grok/xAI calls). Prints per-field accuracy. Foundation only.")
    evalp.add_argument("--cases", required=True, help="JSON file with list of {original_name, source_parent?, expected:{artist,character,target_folder,status,...}}")
    evalp.set_defaults(func=command_eval_namegen)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
