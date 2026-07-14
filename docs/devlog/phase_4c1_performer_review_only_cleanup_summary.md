# Phase 4c.1 Stash Performer Review-Only Cleanup and Final Phase 4c Safety Validation Summary

## Goal
Ensure generic Stash performers like "Hentai" are classified as ignored_or_review (not artist_aliases) using the existing review-only mechanism, completing the preflight audit for performer false positives. This makes the Phase 4c import safe by preventing bad performer entries from being staged via "Import Selected to Manager". Final safety validation that ignored_or_review rows are skipped in staging, no direct writes occur, etc.

## Root Cause
The preflight patch added _get_review_only_reason checks for groups and tags (preventing Hentai, Compilation, Characters, etc. from bad sections), and the performer loop still unconditionally did artist_candidate -> artist_aliases for all stash_performers. Thus, Hentai as performer still showed as missing artist candidate in reports/audits.

## Files Changed
- r34_gui.py (added review check for performers in build_stash_import_preview using _get_review_only_reason before the artist_aliases _make_item; updated get_sample_stash_data to include "Hentai" performer + data + count)
- tests/test_r34_organizer.py (added tests for performer review-only behavior, export, staging skip, no writes, full suite)
- docs/devlog/phase_4c1_performer_review_only_cleanup_summary.md (this file)

## Exact Behavior Changed
In build_stash_import_preview():
- For each performer_info:
  - Compute rev = _get_review_only_reason(orig, aliases/alias_list)
  - If rev: _make_item( source="stash_performer", ..., detected_role="ignored_or_review", suggested_section="ignored_or_review", status="ignored_or_review", classification_reason=rev , forced_status=... )
  - Else: normal artist_candidate / artist_aliases
- This happens for both rich performer_data and flat "performers" list fallback.
- Sample now includes "Hentai" in performers/performer_data so tests cover it.
- Export (existing logic) now puts such rows under "Ignored or review-only" because suggested_section=ignored_or_review (and counts already tracked it).
- stage_stash_import_items() (existing) skips any with suggested_section=="ignored_or_review".
- Normal performers (e.g. "Pantsushi", "Angel Summer") unaffected.

## Tests Run and Results
- python -m py_compile r34_organizer.py r34_gui.py → SUCCESS
- python -m unittest discover -s tests --verbose → Ran 162 tests OK (added 2 specific + safety tests; full suite passes)
- Specific:
  - Hentai as stash_performer → ignored_or_review (source, role, section, status, reason has "denylist")
  - Hentai performer listed in export under Ignored or review-only section
  - Hentai performer skipped by stage_stash_import_items (in skipped["ignored_or_review"])
  - Normal performer still artist_aliases + missing_local
  - No "mutation " in source
  - mtime checks: no r34_config.json or learned write from preview/build/stage/export
  - Full suite meta

## Manual Verification Performed
Using sample data (via python calls simulating the 13 steps, plus GUI launch would be similar):
- Load sample (now has Hentai performer).
- In preview items: Hentai performer has source=stash_performer, detected_role=ignored_or_review, suggested=ignored_or_review, status=ignored_or_review, reason="review-only denylist match: hentai"
- Does not appear under artist_aliases filter (or missing artist counts).
- Appears under ignored_or_review section/filter.
- Selecting it for import: stage skips it (as ignored).
- Normal performers (Pantsushi etc.) still correctly artist_aliases, importable.
- r34_config and learned mtimes unchanged by build/preview/stage (only Save would touch).
- No Stash calls/mutations in the flow.
- Export report has the ignored section with full fields including the performer review row.

## Confirmation Hentai-as-performer is review-only
Yes, forced via the check before artist logic; appears in ignored section, skipped by import staging.

## Confirmation normal performers still map to artist_aliases
Yes, "Pantsushi" etc. get artist_candidate / artist_aliases / missing_local (or exists).

## Confirmation Import Selected skips ignored_or_review rows
Yes, stage_stash_import_items explicitly skips sec == "ignored_or_review" (and ambiguous/exists).

## Confirmation no config/learned writes occurred during preview/import staging
Yes, pure functions only; dedicated mtime tests in suite + manual temp copy asserts pass. Save Changes is still only path.

## Confirmation no Stash mutations were sent
Yes, no change to query code; all read-only; source scan tests pass.

## Confirmation r34_organizer.py was not changed
Yes, not changed (0 edits). All in r34_gui.py pures (build, sample) and tests, per hard rules.

## Whether it is safe for review
Yes. Completes the 4c performer false-positive cleanup on top of preflight. All safety (skips in import, no writes, no mutations, normal behavior preserved) validated in tests and manual. Follows all hard rules. Builds on open 4c PR branch. Ready for ChatGPT review.

**Only Phase 4c.1 scope. 162 tests OK. PR will be updated on existing 4c branch.**