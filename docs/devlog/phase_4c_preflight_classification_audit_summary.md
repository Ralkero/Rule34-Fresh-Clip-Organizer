# Phase 4c Preflight Classification Audit Patch Summary

## Goal
Improve Stash preview classification and reporting for audit before using import/apply (Phase 4c). Fix false positives where non-artist groups (Compilation, Hentai, titles, multis, "Characters" category) were suggested for artist_aliases / canonical_character_aliases, improve duplicate detection with compact keys (flag as possible_duplicate, no auto-merge), ensure ignored/ambiguous rows are listed in export for review, and update export notes/rows to reflect current (post-4b.6) tag classification + full metadata. All as pure preview changes; bulk import / writes / Stash mutations remain untouched.

## Files Changed
- r34_gui.py: added compact_normalize + _get_review_only_reason + REVIEW_* consts; enhanced classify_stash_tag + classify_stash_group (early review check); updated build_stash_import_preview (review check before group override, compact dup logic in _make_item + seen_compact, "compact_key" in items); updated export_stash_preview_report (better fmt with all fields, dedicated ignored + ambiguous sections, updated tag classification notes); updated get_sample_stash_data (added test cases for bad names/dups)
- tests/test_r34_organizer.py: added 10+ preflight audit tests at end of Phase3b... (build/export focused)
- docs/devlog/phase_4c_preflight_classification_audit_summary.md (this)

(No changes to stage_stash_import_items, _import_selected..., UI buttons, or any write paths.)

## Exact Import Behavior
N/A - this patch explicitly does **not** implement or change bulk import (per requirements). Classification fixes make future import safer by reducing bad candidates in preview (and reports).

## Changes to Classification (build + classify)
- review-only denylist (Compilation, Compilations, Cleavage, Discipline, Lubed, SaveAss, Slayed, Hentai) + patterns for comma multis, "x " pairings, maiden/model titles (Bonus Maiden, Maiden N), specific titles (Itadaki etc, Yuffie x Cloud), "Characters"/"Character" category, model-ish (Tetra ( ), Skx' ).
- _get_review_only_reason used in classify_* (return ignored_or_review) and in build group loop (before any role_override force, so even "rule34_artists" override won't promote bad names).
- For "Characters" tag: now ignored even with parent clues suggesting character.
- compact_normalize (strips \s . - _ ( ) ' " etc) added; used alongside norm_key for seen dup detection in _make_item: if norm or compact collides in section -> status=possible_duplicate (not auto anything).
- Items now include "compact_key"; role/section/source/status/reason always populated.
- Bad names now get suggested=ignored_or_review + appropriate role/reason/status (missing or forced).

## Changes to Export
- fmt now always emits: (norm_key, compact if present) [source:...] role:... -> section | status:... | reason:...
- Added:
  ## Ignored or review-only (...) listing all such rows (with full metadata)
  ## Ambiguous (...) for status=ambiguous or role=ambiguous
- possible dups section now focuses on possible_duplicate (ambig separated)
- Updated ## Important Notes: describes current parent/ancestor classification for tags; ignored_or_review for unclassified/review cases (e.g. Characters, bad titles); no longer outdated "all tags are canonical only".
- Counts already had ignored/ambiguous; reports now list the rows (fixes "135 ignored but no list").

## Target / Skipped (unchanged from 4b/4c, but better classified)
Supported suggestions still artist/folder/canonical for good candidates.
Bad ones now correctly skipped to ignored_or_review (visible in reports).

## Tests Run and Results
- python -m py_compile r34_organizer.py r34_gui.py → SUCCESS
- python -m unittest discover -s tests --verbose → **Ran 160 tests in 4.716s OK** (prior 150 + ~10 new preflight; all pass)
- New tests (all pure, use build/export on custom/sample data, tmp for reports, no net, no writes):
  - test_preflight_compilation_group_is_review_only
  - test_preflight_hentai_is_review_only (even under rule34 override)
  - test_preflight_characters_tag_is_review_only
  - test_preflight_comma_multi_is_review_only
  - test_preflight_x_pairing_is_review_only
  - test_preflight_compact_key_duplicate_flagged_possible_duplicate (Aries Possession + AriesPossession)
  - test_preflight_export_includes_ignored_or_review_section (and lists review reasons)
  - test_preflight_export_includes_ambiguous_section
  - test_preflight_export_note_reflects_tag_classification (has "classified by parent/ancestor", no old "candidates only")
  - test_preflight_export_rows_include_full_fields (role, section, source, status, reason, compact)
- Existing dup/export/no-mutation/full-suite tests continue to pass (enhanced dup uses compact too).

## Manual / Report Verification
- Used python to build preview with bad cases (Compilation etc, comma, x, Characters, dups) + export to tmp; asserted sections present, rows have full fields, reasons match, notes updated.
- Confirmed ignored now listed (was count-only), ambig listed, compact dups flagged.
- No impact on 4c import staging (which still skips ignored/ambig/exists by design; now gets cleaner preview items).

## Confirmation No Unrelated Behavior Changed
- All prior 4b.6 classification (parents, group override, role/section sep), 4c in-mem staging, Save path, filters, etc. untouched.
- normalize_stash_name unchanged (compact is additive for dups only).
- Export still produces timestamped .md to cwd/tmp, redacted, read-only notes.
- sample data extended but backward compatible (old tests pass).

## Confirmation No Bulk Import / Writes / Mutations
- Explicitly no changes to import UI/logic (stage_ , _import_*, buttons, Save integration).
- All changes pure (build/classify/export); tests use temp for reports only, assert no config mtime change from preview/export.
- No GraphQL beyond existing read-only; source still has no "mutation ".
- Phase 4c import remains as-is (reviewed, in-mem only).

**Only preflight classification + audit report improvements. 160 tests OK. r34_organizer.py untouched. Safe to proceed with 4c import on cleaned data. (See also phase_4c_stash_import_to_manager_summary.md)**