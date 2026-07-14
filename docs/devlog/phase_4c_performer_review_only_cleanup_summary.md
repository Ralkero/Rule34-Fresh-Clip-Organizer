# Phase 4c Small Classification Fix: Performer Review-Only Cleanup Summary

## Goal
The preflight patch (phase_4c_preflight...) added review-only denylist/pattern handling for Hentai (and similar) in group/tag paths, but stash_performer rows were still unconditionally forced to artist_aliases candidates (even for generic/denylist names like "Hentai"). This caused false positives in preview reports (e.g. "Hentai" listed as missing artist candidate). Fix: apply the existing _get_review_only_reason() check to performers too, before artist classification.

## Files Changed
- r34_gui.py (added review check in the stash_performer loop inside build_stash_import_preview, before _make_item; updated get_sample_stash_data to include "Hentai" as performer + rich data + adjusted counts for test coverage)
- tests/test_r34_organizer.py (added 2 new tests at end of Phase3bKnownValuesConfigEditTests)
- docs/devlog/phase_4c_performer_review_only_cleanup_summary.md (this file)

(No changes to import/stage logic, _import_selected..., UI, export, or any write paths.)

## Exact Change
In build_stash_import_preview (after rich-data fallback for performers):
```python
for p in performer_infos:
    orig = p.get("name") or ""
    if not orig: continue
    # Small 4c classification fix: apply review-only check to performers too...
    rev = _get_review_only_reason(orig, p.get("aliases") or p.get("alias_list"))
    if rev:
        _make_item("stash_performer", orig, "ignored_or_review", "", "ignored_or_review", rev, forced_status="ignored_or_review")
        continue
    _make_item("stash_performer", orig, "artist_aliases", "", "artist_candidate", "Stash performer")
```
- Uses the pre-existing _get_review_only_reason + REVIEW_ONLY_DENYLIST (which includes "hentai").
- If match: source=stash_performer, detected_role=ignored_or_review, suggested_section=ignored_or_review, status=ignored_or_review (via forced), classification_reason= the review reason (e.g. "review-only denylist match: hentai").
- Normal performers (e.g. "Pantsushi") continue to artist_aliases as before.
- Review check happens early, independent of any group override (performers have no override).

## Test Added + Results
- `test_preflight_hentai_performer_is_review_only_not_artist`: custom stash with Hentai performer -> asserts source/detected_role/suggested_section/status=ignored_or_review and reason contains "review-only denylist match".
- `test_preflight_normal_performer_still_artist_alias`: custom with "Pantsushi" -> asserts suggested_section=artist_aliases, status=missing_local.
- Overall: python -m py_compile ... → SUCCESS; python -m unittest discover -s tests → **Ran 162 tests in 4.887s OK** ( +2 from this fix; prior 160 including all 4c/preflight tests still pass; no regressions).

## Manual / Behavior Verification
- Used python REPL-style calls (build on custom + sample) + asserts in the new tests: Hentai performer now ignored (not in artist_missing counts), normal ones still artist.
- Sample now includes Hentai as performer (and data), so future report runs will reflect correct classification.
- Confirmed via build that "Hentai" now gets the review fields exactly as specified.

## Confirms (per requirements)
- Do not change import behavior: untouched (stage_ / _import_ / UI / Save path unchanged; import still skips ignored_or_review rows by design).
- Do not write config/learned files: pure classification only (no apply_ calls, no json, no mtime changes; tests use in-mem only).
- Do not mutate Stash: no queries/mutations added (still read-only find* only).
- r34_organizer.py untouched: 0 edits (all in r34_gui.py pures + sample, per hard rules).
- Full suite still passes (162 OK).
- Only this small targeted fix (no scope creep).

## Commands Run
- python -m py_compile r34_organizer.py r34_gui.py → SUCCESS
- python -m unittest discover -s tests --verbose → Ran 162 tests ... OK

**Only this tiny performer review-only extension for 4c preflight completeness. Prior 4b/4c behavior (including group/tag review) preserved. Safe, minimal, testable.**