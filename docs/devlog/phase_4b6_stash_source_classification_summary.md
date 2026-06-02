# Phase 4b.6 Stash Source Classification and Group-Role Override Patch Summary

## Root Cause
Previous classification (4b.5/4b.6 tag work) tied source type directly and rigidly to detected role/suggested section:
- stash_performer always -> artist_candidate / artist_aliases
- stash_group always -> franchise_candidate / folder_aliases
- stash_tag -> (classified) character etc.

This broke for users where Stash "groups" represent Rule34 artists/creators (not franchises), causing source="stash_group" rows to appear only under folder_aliases even when they are artists, and section filter "artist_aliases" missing them. No user control to override the assumption for groups.

## Exact Source-vs-Role-vs-Section Changes
- source: always reflects origin (stash_performer / stash_group / stash_tag) -- unchanged.
- detected_role: now independent (artist_candidate, franchise_candidate, character_candidate, ignored_or_review, ambiguous)
- suggested_section: independent (artist_aliases, folder_aliases, canonical_character_aliases, ignored_or_review)
- classification_reason / note: updated with user config when override used.
- In build_stash_import_preview: added param `group_role_override: str = "auto"`
  - "rule34_artists" -> role=artist_candidate, section=artist_aliases, reason="User configured Stash Groups as Rule34 artists"
  - "franchises" -> role=franchise_candidate, section=folder_aliases, ...
  - "ignore_review" -> role=ignored_or_review, section=ignored_or_review, ...
  - "auto" (default) -> franchise_candidate / folder_aliases (preserves prior for groups)
- _make_item updated to accept/pass the role/reason; defaults adjusted.
- UI: new "Treat Stash Groups as:" Combobox (values: auto, franchises, rule34_artists, ignore_review) in the preview panel.
  - Stored in group_role_var (local to cat creation).
  - Passed on Load (sample or live): build_...(..., group_role_override=group_role_var.get())
  - Requires explicit "Reload Preview" after change (per task).
- Filters unchanged in behavior but now reflect overrides correctly:
  - Source filter: filters on .source (still shows stash_group even if role is artist)
  - Section filter: filters on .suggested_section (so artist_aliases now pulls in overridden stash_group artist rows)
  - Role filter: on .detected_tag_role
- Preview rows include full fields; display logic in _repop already shows role/section.
- Tags: classification from prior (parent-based) preserved; no change to auto character assumption beyond that.
- No other behavior changes.

## New Group-Role Override Behavior
As specified exactly in the query for the three non-auto cases (with "auto" as safe default preserving old).

## Real Stash Counts Before/After (if tested)
Real Stash (localhost:9999, connected True):
- Default (auto/franchises): 247 stash_group rows with suggested_section=folder_aliases
- With "rule34_artists" override: 247 stash_group rows with suggested_section=artist_aliases (same rows, reclassified)
- Source filter "stash_group" still shows them; section "artist_aliases" includes the reclassified groups; "folder_aliases" does not.

## Confirmation stash_group can map to artist_aliases when configured
Yes: when override="rule34_artists", groups get artist_candidate / artist_aliases, and appear in artist_aliases section filter while source remains stash_group. Existing local matches marked already_exists_local against artist_aliases.

## Confirmation No Config/Learned Writes Occurred
Yes: build is pure (in-memory only). No apply helpers called. Safety test in suite checks mtime on temp config copy unchanged. UI only reads edit_ dicts for comparison.

## Confirmation No Stash Mutations Were Sent
Yes: query still only does find* / version (read-only). No mutations in code (source scan tests pass).

## Confirmation Phase 4c Import/Apply Remains Deferred
Yes: disabled button + "Phase 4c - not implemented" label + notes remain. No import logic added or enabled.

## Tests Run and Results
- python -m py_compile r34_organizer.py r34_gui.py → 0
- python -m unittest discover -s tests --verbose → Ran 130 tests in 4.733s **OK** (added 9 new tests covering exactly the required 1-9 points for group override + safety/no-mutation/meta; all pass; no breakage to prior classification tests)
- Tests added/updated cover:
  1. group + "rule34_artists" → source=stash_group, role=artist_candidate, section=artist_aliases + reason
  2. + "franchises" → role=franchise, section=folder + reason
  3. + "ignore_review" → role=ignored, section=ignored + reason
  4. section artist_aliases includes stash_group rows under rule34_artists override
  5. section folder_aliases does not include those artist-group rows
  6. existing local artist aliases marked already_exists_local for group-as-artist
  7. no config writes (mtime test)
  8. no mutation strings
  9. full suite meta after changes

**Only Phase 4b.6 scope (source/role/section separation + group UI override for preview classification). Read-only. 4c deferred. r34_organizer.py untouched.**