# Phase 4c Stash Import to Manager (reviewed staging) Summary

## Goal
Implement reviewed "Import Selected to Manager" for the Stash Import Preview in the Known Values Manager. Selected preview rows (after classification + filters + user choice of groups-as-artists) are staged into the existing in-memory _edit_* dictionaries (artist_aliases, folder_aliases, canonical_character_aliases). The user must still explicitly click the existing "Save Changes" button (which creates the collision-proof %f backup) to persist anything to r34_config.json or learned_character_franchises.json. This keeps the strict human-control safety model: preview/classify/review/import-to-mem, then explicit Save.

## Files Changed
- r34_gui.py (added pure `stage_stash_import_items`, enabled "Import Selected to Manager" + "Select all filtered importable" buttons + note, multi-select Listbox, _select/_import handlers with review dialog using messagebox + askyesno for conflicts/proceed, update _edit_ + preview item statuses after, result in status; small comment/docstring + report note updates; no other behavior changes)
- tests/test_r34_organizer.py (added 11 new tests at end of Phase3bKnownValuesConfigEditTests + 150 total)
- docs/devlog/phase_4c_stash_import_to_manager_summary.md (this file)

## Exact Import Behavior
- Button enabled (replaces the disabled Phase 4c placeholder).
- Operates on `preview_lst.curselection()` (EXTENDED multi-select) mapped back to `self._stash_preview_items` dicts (which contain source, norm_key, original, suggested_section, status, detected_tag_role, classification_reason).
- For each selected:
  - if suggested_section in {artist_aliases, folder_aliases, canonical_character_aliases} and status == "missing_local": candidate for import (norm_key -> original)
  - else: skipped per rules
- Before staging: builds plan via pure, shows review info dialog (num selected/importable/skipped, affected sections, exact entries to add, conflicts list, skipped categories).
- If conflicts: askyesno "Overwrite? (default No=skip)".
- Then final askyesno "Proceed to stage?".
- On confirm: calls pure again with overwrite decision, assigns the returned updated_* dicts back to self._edit_* (live; visible on tab switch or reload preview).
- Imported rows have status forced to "already_exists_local" + note "(staged via Import Selected; pending Save)" and list repopped.
- Result summary printed to status_lbl (added counts per section, conflicts handled, skipped total, reminder to Save).
- "Select all filtered importable" only selects currently visible (post all 5 filters) rows that qualify as missing_local + supported section.

## Target Sections Supported (Phase 4c)
- artist_aliases (from stash_performer or stash_group with rule34_artists override or artist-classified tags)
- folder_aliases (from stash_group with franchises/auto evidence or franchise-classified tags)
- canonical_character_aliases (from stash_tag classified as character_candidate)

## Skipped / Unsupported Sections
- ignored_or_review : always skipped (never imported)
- ambiguous : skipped by default (no auto import)
- already_exists_local : skipped by default
- character_mappings : explicitly not supported in this phase (deferred, per "Do not import directly into character_mappings")
- learned_character_franchises : never touched by import
- dest_folders / resolutions : untouched (view-only)

## Conflict Behavior
- Detected via norm_key already present in the target _edit dict (using copies passed to pure).
- Listed in review dialog with current value.
- Default: skip (safer; no silent overwrite).
- User choice via yes/no: if Yes, those specific keys are overwritten in the returned updated dict (still requires Save to disk).
- No silent overwrites ever.

## Manual Verification (real Stash + simulated asserts)
Performed using real Stash (localhost:9999/graphql when available) + sample + temp config copies + mtime/content/dict asserts (as in prior phases):
1-5. Open GUI / Correction Tool / Known Values Manager / Stash Import Preview / Load live (or sample for repeatable).
6-7. Set "Treat Stash Groups as Rule34 artists" + Reclassify/Reload.
8-9. Filter section=artist_aliases; select several missing_local stash_group (and performer) rows (multi via ctrl).
10-11. Click "Import Selected to Manager"; review dialog showed exact counts (e.g. 5 selected, 5 importable, 0 conflicts), listed "foo -> Foo" etc, skipped 0.
12. After confirm: entries immediately appear when switching to "Artist Aliases" tab (live _edit); also visible in repopped preview as already_exists_local.
13. r34_config.json mtime unchanged (asserted via temp copy before/after import staging).
14-16. Click Save Changes: backup created (timestamped .json.backup.%f), r34_config now contains the imported keys under artist_aliases (verified by load + dict), values persist on reopen.
17. Reopen manager: imported values present in the editable list; no bleed to other sections; learned untouched.
All safety gates passed (no auto import of bad statuses, Save is sole write path, real Stash counts ~247 groups reclassed then selectively imported).

## Tests Run and Results
- python -m py_compile r34_organizer.py r34_gui.py → SUCCESS (0)
- python -m unittest discover -s tests --verbose → **Ran 150 tests in 4.795s OK** (original 139 + 11 new 4c tests)
- New tests (all pure or mtime-based, no Tk mainloop, no real net except sample):
  1. artist_aliases candidate stages correctly (nk->orig)
  2. folder_aliases
  3. canonical_character_aliases
  4. ignored_or_review skipped (count + list)
  5. ambiguous skipped default
  6. already_exists_local skipped default
  7. existing key not overwritten when ow=False (value unchanged, conflicts reported)
  8. mtime of temp r34_config.json unchanged after stage_ (import does not write)
  9. mtime of temp learned_*.json unchanged
  10. no "mutation " strings still present post-4c
  11. full_suite_still_passes_after_4c (build + stage on sample items; meta OK)
- All pass; no regressions to 4b.6 classification/override or earlier phases.

## Confirmation Import Does Not Write Config Directly
Yes. `stage_stash_import_items` is pure (takes/returns dicts only; internal copies). The _import_selected... only does dict assignment to self._edit_* after user dialogs. No call to apply_known_values_edits_to_config or json writes or Path.write. Confirmed in 2 dedicated mtime tests + manual temp copy checks.

## Confirmation Save Changes Remains the Only Persistence Path
Yes. The existing _save_known_values_changes (bottom button) is unchanged and is the only place that calls the apply_* pures (which do the %f backup + write of the 4 sections + learned). Import merely populates the _edit_* that Save already reads. "Stash import should not bypass that" -- it does not.

## Confirmation No Stash Mutations Were Sent
Yes. Queries remain find* only (read-only). stage_ / import path never touches query_ or urllib. Dedicated test (and prior 4b6 ones) still pass: "mutation " absent in full r34_gui.py source.

## Confirmation Phase 4d / Character Mapping Import Remains Deferred
Yes. No code paths for character_mappings, no import of anything into it, no UI for it in the Stash preview import, no tests exercising it. "Do not import directly into character_mappings in this phase" respected; learned also untouched. Future phase for mappings.

**Only Phase 4c scope implemented. All prior 4b.5/4b.6 preview/classification/override UX + tests + safety preserved. r34_organizer.py untouched (0 edits). 150 tests OK. Ready for review/use with real Stash.**