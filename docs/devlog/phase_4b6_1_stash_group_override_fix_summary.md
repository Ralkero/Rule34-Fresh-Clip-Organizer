# Phase 4b.6.1 Stash Group Auto-Classification and Override Fix Summary

## Root Cause
In the Phase 4b.6 implementation, the `group_role_override` logic in `build_stash_import_preview` correctly handled explicit "rule34_artists"/"franchises"/"ignore_review", and the UI dropdown + passing of `group_role_var.get()` was present. However:
- The "auto" branch unconditionally defaulted Stash groups to `franchise_candidate` / `folder_aliases` ("Stash group (auto)"), ignoring the requirement for evidence-based classification in auto mode.
- No `classify_stash_group` for auto (unlike tags).
- The UI dropdown label/note was outdated, no change-warning label, no reclassify-without-reload button.
- This caused user's Stash groups (which are R34 artists, no franchise evidence) to still show as franchise -> folder even in "auto", and possibly override not fully taking effect in all paths (e.g. reclass button missing).
- Filters and _make_item status for ignore weren't forcing "ignored_or_review" status.
- Legacy default in _make_item for groups was franchise if None.

## Exact Override Behavior Fixed
- Explicit overrides now always respected in the group loop (before _make_item).
- For "ignore_review": `forced_status="ignored_or_review"` passed to _make_item (which now supports it), so status set correctly regardless of exists/dup calc.
- Added `classify_stash_group` pure helper using name+aliases vs franchise-like keywords (same fran_kws as tags + "studio").
- Updated auto else: `role, sugg, reason = classify_stash_group(g)` instead of hard franchise.
  - If evidence (e.g. "Franchise" or "Game" in name): franchise / folder
  - No evidence: ambiguous / ignored_or_review + "Auto mode found no reliable group role evidence"
- `_make_item` enhanced with `forced_status` param for the ignore case.
- Verified dropdown value is passed: in _load_stash_preview(..., group_role_override=group_role_var.get() ) and also in _reclassify_current_preview.

## Exact Auto Behavior Changed
- Auto no longer blindly maps *all* groups to folder_aliases.
- Only groups whose name/alias contains franchise/game/series/universe/studio keywords get franchise_candidate/folder_aliases.
- Generic artist-like groups (e.g. "RandomArtistCollective", user's R34 artist groups) now get ambiguous/ignored_or_review in auto.
- In real Stash run: auto produced only 3 folder_groups (evidence-based ones), while 247 artist-groups went to ambiguous/ignored until override selected.

## Whether Real Stash Was Tested
Yes. Manual verification used direct calls + live query to localhost:9999/graphql (connected True, no key).
- Auto: only evidence-based groups (e.g. 3) to folder_aliases; generic to ambiguous/ignored.
- Override "rule34_artists": 247 stash_group rows reclassified to artist_candidate/artist_aliases (source remains stash_group).
- Confirmed section "artist_aliases" includes the 247, "folder_aliases" shows 0 for them.

## Counts by Source/Role/Section After Selecting Rule34 Artists (real)
- source=stash_group with rule34_artists: 247
- detected_role=artist_candidate for those: 247 (in artist_aliases)
- suggested_section=artist_aliases for those groups: 247
- folder_aliases for stash_group under this override: 0 (for the artist ones)
- (Other roles/sections from tags/performers unchanged.)

## Confirmation stash_group can map to artist_aliases
Yes: when override="rule34_artists", all source=stash_group become artist_candidate/artist_aliases + matching reason. Section filter artist_aliases shows them; folder does not. Role artist_candidate includes them. Source filter still lists as stash_group.

## Confirmation auto no longer blindly maps groups to folder_aliases
Yes: generic groups -> ambiguous/ignored_or_review with the exact "Auto mode found no reliable..." reason. Only keyword-matching groups get franchise/folder in auto. Real data showed only 3 (evidence) in folder under auto.

## Confirmation No Config/Learned Writes Occurred
Yes. All pure (build returns data only). Added test asserts mtime unchanged on temp config. No apply paths, no FS in reclassify (uses in-mem last_raw).

## Confirmation No Stash Mutations Were Sent
Yes. Queries unchanged (still find* only). Source scan + dedicated tests confirm no "mutation " strings.

## Confirmation Phase 4c Import/Apply Remains Deferred
Yes. Button still disabled with "Phase 4c - not implemented" label and notes. Reclassify is purely for preview re-calc, no import.

## Tests Run and Results
- `python -m py_compile r34_organizer.py r34_gui.py` → 0
- `python -m unittest discover -s tests --verbose` → **Ran 139 tests in 4.680s OK**
- New tests added (test_auto_no_evidence_..., test_auto_with_evidence_..., plus the 7 override/section ones from 4b.6 + safety):
  - auto no-evidence -> ambiguous/ignored (not franchise)
  - auto with evidence still franchise
  - explicit overrides for rule34/franchise/ignore set correct role/section/status/reason
  - section artist includes group-under-rule34; folder excludes
  - no mutations
  - full suite meta
- All pass. Existing tests (e.g. default auto on sample) still pass (sample has evidence groups that auto classifies to franchise, as intended).

**Only 4b.6.1 scope (fix auto + ensure override + UI polish for group treatment). Read-only preview. 4c deferred. org.py untouched (0 edits).**