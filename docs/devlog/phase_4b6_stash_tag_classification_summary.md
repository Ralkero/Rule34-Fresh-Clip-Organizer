# Phase 4b.6 Stash Tag Classification Preview Fix Summary

## Root Cause
In Phase 4b.5, all Stash tags fetched via findTags were unconditionally mapped to `canonical_character_aliases` candidates (with a static note). This caused Rule34 artist/creator tags (stored as tags in Stash, not performers) to appear only under "all" or canon section instead of artist_aliases when filtered. Stash tags are multi-purpose (characters, artists, franchises, general), so blind mapping to characters is unsafe for future import/apply. No parent/ancestor data was fetched or used for classification.

## Exact Classification Rules Implemented
Pure `classify_stash_tag(tag_info: dict) -> (detected_tag_role, suggested_section, classification_reason)`:

- Collect clues from tag.name/aliases + direct parents (name + aliases) + grandparent names (limited recursion for ancestor support).
- artist_candidate -> artist_aliases if any clue matches: artist/artists/creator/creators/r34 artist/rule34 artist/animator/animators (substring match in lower).
- character_candidate -> canonical_character_aliases if matches: character/characters.
- franchise_candidate -> folder_aliases if matches: franchise/franchises/series/game/games/source/sources/universe.
- If >1 category matches: ambiguous -> ignored_or_review.
- If has parents/ancestors but no match: general_tag -> ignored_or_review.
- If no clues: general_tag -> ignored_or_review.
- Performers: always artist_candidate -> artist_aliases (reason: "Stash performer").
- Groups: always franchise_candidate -> folder_aliases (reason: "Stash group").

Classification happens in build before assigning suggested_section. New fields added to every item:
- detected_tag_role
- classification_reason
- (source, original, norm_key, suggested_section, status, note remain; note may hold reason for compat)

Section filter now correctly routes via suggested_section (artist_aliases filter shows performers + artist_candidate tags, etc.). ignored_or_review added as filterable section.

## Stash Fields Queried (updated in query_stash_readonly)
- findPerformers( per_page:-1 ): count, performers { id name alias_list }
- findGroups( per_page:-1 ): count, groups { id name aliases }
- findTags( per_page:-1 ): count, tags { id name aliases parents {id name} children {id name} }
- Fallbacks for groups (studios/movies) kept (limited fields).
- Also populates *_data rich lists (performer_data, group_data, tag_data) + kept flat name lists + query_status/response_counts for compat/UI/debug.
- All read-only.

## Whether Real Stash Was Tested
Yes. Manual verification ran against live localhost:9999/graphql (no key). Also full sample with crafted parents for edge cases.

## Counts by Source Type (from manual real + sample)
- Sample: stash_performer:5 , stash_group:3 , stash_tag:7  (total 15 items)
- Real (example run): ~18 performers + groups + 100s tags (total 457 items classified)

## Counts by Detected Role (real example from manual)
- artist_candidate: 18 (performers + classified artist tags)
- character_candidate: 57
- franchise_candidate: 247
- general_tag: 135
- ambiguous: 0

## Counts by Suggested Target Section (sample)
- artist_aliases: 6 (5 perf + 1 artist_tag)
- folder_aliases: 4 (3 groups + 1 franchise_tag)
- canonical_character_aliases: 3
- ignored_or_review: 2 (1 general + 1 ambiguous)

(Real similar proportions, with many general/ignored.)

## Confirmation No Config/Learned Writes Occurred
Yes. Pure functions only (query returns data, build classifies in-memory, UI displays). No apply_*, no file writes except optional export report to validation_tmp (as before). Manual + tests confirmed.

## Confirmation No Stash Mutations Were Sent
Yes. All queries are find* or version probe. Source checks + tests assert no "mutation " or mutation{ in r34_gui.py. UI is read-only preview.

## Confirmation Phase 4c Import/Apply Remains Deferred
Yes. Disabled button remains: "Import Selected (Phase 4c - not implemented)". Labels/notes throughout UI and code state "read-only preview + export only". No wiring for import.

## Test Results
- Commands:
  python -m py_compile r34_organizer.py r34_gui.py  # 0
  python -m unittest discover -s tests --verbose    # Ran 121 tests in 4.676s **OK**
- 11+ required points covered by new tests (tag parent "Artists" -> artist_cand/artist_aliases; "Characters"->char_cand/canon; "Franchises"->fran_cand/folder; no clue->general/ignored; conflicting->ambiguous/ignored; artist_aliases section includes perf+artist_tags; canon does not include artist_tags; already_exists still works; no mutations; no real server needed; full suite meta).
- All pass cleanly. Existing tests updated minimally for new reasons in sample (still verify no auto-mappings to character_mappings, tags go to canon when appropriate).
- r34_organizer.py: untouched (0 edits, as required).

**Only Phase 4b.6 scope (read-only classification preview fix). Safe to proceed. 4c still fully deferred.**