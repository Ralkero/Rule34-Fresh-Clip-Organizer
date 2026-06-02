# Known Values Manager Editable Categories Bugfix Summary

## Root Cause
In the Phase 4a.5 sidebar refactor, the `cats` list was updated to use stable internal keys like `("artist_aliases", "Artist Aliases")` and `("folder_aliases", "Folder Aliases / Franchises")` (to match the `self._edit_*` dict names and avoid using display labels as logic).

However, the `if cat in (...)` condition and inner `if/elif cat == ...` branches inside `_switch_category` were never updated from the old internal names ("artists", "franchises"):

```python
if cat in ("artists", "franchises", "character_mappings", ...):
    if cat == "artists":
        ...
    elif cat == "franchises":
        ...
```

When the left Listbox selection calls `_on_cat_select` -> `cat = self._cat_map.get(disp)` (which returns "artist_aliases" for "Artist Aliases"), then `_switch_category(cat=...)`:

- "artist_aliases" did not match the if condition (mismatch).
- It fell through to `elif cat == "dest_folders":` / `elif cat == "resolutions":` / `else:` (the characters/res view-only block).
- In the else: `src = (self.correction_known or {}).get(cat, [])` -> empty (no "artist_aliases" key in correction_known), plus the view-only note was shown.
- Result: Artist Aliases and Folder Aliases appeared empty + labeled view-only, with no editable controls (Add New etc.).

The other categories (character_mappings etc.) happened to use matching keys so they worked by accident. The first two (the aliases ones) were broken by the key change in 4a.5 without updating the routing logic.

This was a classic "display label vs internal ID" + incomplete refactor bug.

## Files Changed
- `r34_gui.py` (only file edited)

## Exact Category Routing Fix
1. Updated the outer condition to use the actual internal keys from the `cats` list:
   ```python
   if cat in ("artist_aliases", "folder_aliases", "character_mappings", "canonical_character_aliases", "learned"):
   ```

2. Updated the inner branches to match the keys used in `cats` and the edit dict loading:
   ```python
   if cat == "artist_aliases":
       edit_d = self._edit_artist_aliases
       ...
   elif cat == "folder_aliases":
       edit_d = self._edit_folder_aliases
       ...
   elif cat == "character_mappings":
       ...
   elif cat == "canonical_character_aliases":
       ...
   else:  # learned
       ...
   ```

3. (The `else` for view-only dest/res/characters was left as-is, since their keys "dest_folders", "resolutions", "characters" are still handled by the subsequent `elif`/`else`.)

4. Added (at build time, inside the now-correct editable branch) an empty-state message for the case when the loaded edit_d is empty:
   ```python
   if len(edit_d) == 0:
       empty_msg = f"No local {tab_title.lower()} yet. Use Add New to create one, or import from Stash in a later phase."
       ttk.Label(parent, text=empty_msg, wraplength=600, foreground="gray").pack(anchor="w", pady=2)
   ```
   This is shown *above* the controls, but the Listbox + filter + 4 buttons (Add New / Update Selected / Remove Selected / Clear Selection / New Entry) + entries + count label are *always* rendered for these categories. No "view-only" note or path is taken.

5. No other logic changes; the `_repop_list`, `_do_*` handlers, filters, live refresh, count updates, etc. are the same code that was already working for the other editable categories.

6. Used stable internal IDs throughout the routing (as required). Display labels ("Artist Aliases") are only used for the left Listbox text and the right header.

## Confirmation Artist Aliases is editable
Yes. "Artist Aliases" (display) now correctly maps via `_cat_map` to internal ID "artist_aliases", which now matches the `if`/`if cat == "artist_aliases":` and renders the full editable UI (4 buttons, entries, count label "Artists: X local entries", filter, list, live refresh on add/update/remove/clear, select-to-populate, no view-only message).

## Confirmation Folder Aliases / Franchises is editable
Yes. "Folder Aliases / Franchises" maps to "folder_aliases", hits the editable branch, shows full controls + count "Folder aliases: X local entries", etc. (Same as above.)

## Confirmation empty editable lists still show controls
Yes. When `len(edit_d) == 0` at open, the helpful message is shown, but the Listbox (empty), filter entry, 2 input fields, 4 action buttons, and count label ("...: 0 local entries") are still created and visible/packed. User can immediately use Add New etc. The view-only path is never taken for these categories.

## Confirmation Destination Folders and Resolutions remain view-only
Yes. Their keys ("dest_folders", "resolutions") were never part of the editable `if`; they continue to the `elif cat == "dest_folders":` / `elif cat == "resolutions":` (or the final else for characters), which render only Listboxes + Refresh buttons + (for dest) the 3e Generate/Create suggestions UI + view-only labels/notes. No Add New / Update / Remove / input fields / "editable" controls are present for them.

## Test / Verification Results
- `python -m py_compile r34_organizer.py r34_gui.py` → success (clean).
- `python -m unittest discover -s tests --verbose` → Ran 95 tests in ~0.6s **OK** (exit 0). No regressions (the fix is purely routing strings inside the GUI method; data/save paths untouched; no new tests needed for this string fix, but full suite re-run confirms).
- Source verification (via temp script + grep): confirmed the if/elif/cats now use correct keys, old mismatch strings removed.
- Manual simulation of routing: "artist_aliases" and "folder_aliases" now take the editable path (with controls + empty msg if applicable); "dest_folders"/"resolutions" stay in view-only branches.
- No Stash import implemented (only a static message string).
- r34_organizer.py untouched.
- No changes to Destination Folders or Resolutions editability.

The bug is fixed; the two alias categories now correctly render as editable (with controls even when empty) while preserving all other categories' behavior.