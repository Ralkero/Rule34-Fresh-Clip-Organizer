# Rule34 Fresh Clip Organizer

Local Python CLI for previewing and applying safe renames/moves for freshly downloaded Rule34 clips.

The tool is intentionally two-step:

1. `preview` recursively scans one source folder and writes an editable CSV plan plus a Markdown summary.
2. `apply` reads the reviewed CSV and moves only approved rows.

It does not talk to Stash. After files are moved, rescan/import in Stash separately.

## Requirements

- Python 3.10+
- `ffprobe` on PATH
- A curated destination library folder

Default destination root:

```text
E:\James' Stuff\Rule34
```

## Usage

### One-Click Launchers

Use the Windows launchers when you do not want to type commands:

```text
Preview Rule34 Batch.cmd
Apply Approved Rule34 Plan.cmd
```

Preview:

- Drag a fresh download folder onto `Preview Rule34 Batch.cmd`, or double-click it and choose a folder.
- The launcher runs `preview`.
- It opens the generated CSV plan and Markdown summary.
- Review the CSV before applying.

Apply:

- Drag the reviewed `r34_preview_*.csv` onto `Apply Approved Rule34 Plan.cmd`, or double-click it and choose the CSV.
- The launcher shows how many rows apply will process, including approved imports and content-review holds.
- Type `APPLY` to confirm.
- It runs `apply`, shows percentage progress row by row with `original -> target` filenames, and opens the generated `r34_apply_*.csv` log.

Optional:

```text
Install Desktop Shortcuts.cmd
```

This creates desktop shortcuts named `Rule34 Preview` and `Rule34 Apply Approved Plan`.

### Command Line

Preview a fresh download folder:

```powershell
python r34_organizer.py preview --source "C:\path\to\fresh batch"
```

During preview, the script samples the existing destination library at `E:\James' Stuff\Rule34` and mimics its established filename structure. When a strong character match is found, the target pattern is:

```text
Artist - Canonical Character - Clean Descriptive Title [Resolution].mp4
```

If no strong character match is found, it falls back to:

```text
Artist - Clean Descriptive Title [Resolution].mp4
```

Bracketed resolution labels are normalized to the preferred house style: all letters are capitalized, such as `[1080P]`, `[720P]`, and `[480P]`. The script does not output `[1440P]`; videos in that tier are labeled `[4K]`.

Canonical character names come from `canonical_character_aliases` in `r34_config.json` and from already-standardized filenames in the destination library. For example, `Zelda` and `BotW Zelda` can become `Princess Zelda`, while `Ranni` can become `Ranni the Witch` after that form exists in the library.

Date-prefixed artist batches such as `Nodu 2023` are handled as source-artist folders. Filenames beginning with `YYYY-MM-DD` keep the date out of the artist/title fields, so a file like `2023-01-26 - A Playful Goddess - (Palutena)_4K60fps.mp4` previews as `Nodu - Palutena - A Playful Goddess [4K].mp4`.

Collection source folders are also handled as artist context. For example, `Lazy Procrastinator Collection\2B - Cowgirl.mp4` previews as `Lazy Procrastinator - 2B - Cowgirl [1080P].mp4`, and nested generic folders such as `SageOfOsiris collection\Animations\...` use the parent collection name as the artist. Known artist prefixes still win, so `Pantsushi - ...` remains `Pantsushi`.

Content-review terms in `r34_config.json` hold configured non-vanilla/fetish keywords out of the main library. Preview marks matching rows as `content_review`, and apply moves them to `_r34_content_review\<run-id>` under the source root for manual review.

Known compact title tokens can be expanded through `title_token_replacements` in `r34_config.json`, such as `bonusmotion` -> `Bonus Motion` and `kitchenmissionary` -> `Kitchen Missionary`.

This writes files like:

```text
r34_preview_YYYYMMDD-HHMMSS.csv
r34_preview_YYYYMMDD-HHMMSS.md
```

Review/edit the CSV. Rows with `approved` set to `yes`/`true`/`1` and a non-blocked `status` move to the library. Rows with `status=content_review` move to the source-root content review folder.

Apply the reviewed CSV:

```powershell
python r34_organizer.py apply --plan "C:\path\to\r34_preview_YYYYMMDD-HHMMSS.csv"
```

## CSV Plan Columns

- `approved`
- `source_path`
- `original_name`
- `artist`
- `character`
- `character_confidence`
- `character_reason`
- `clean_title`
- `resolution`
- `target_folder`
- `target_filename`
- `target_path`
- `confidence`
- `status`
- `reason`
- `notes`

## Safety Behavior

- Processes `.mp4` only in v1.
- Never overwrites existing destination files.
- Uses `ffprobe` to read actual video resolution.
- Samples the existing Rule34 library for destination folders, artist precedent, franchise precedent, and canonical character naming precedent.
- Normalizes all generated bracketed resolution tags to uppercase house style; 1440-tier files are labeled `[4K]`.
- Can create missing destination folders when `allow_create_destination_folders` is `true`; only folders named by explicit config mappings are eligible.
- Unapproved rows stay in place by default.
- Approved rows with destination conflicts are moved to `_r34_review/<run-id>` inside the source folder.
- Apply writes `r34_apply_<run-id>.csv` with one result per row.

## Development Checks

Run tests:

```powershell
python -m unittest discover -s tests
```

Run syntax check:

```powershell
python -m py_compile r34_organizer.py
```
