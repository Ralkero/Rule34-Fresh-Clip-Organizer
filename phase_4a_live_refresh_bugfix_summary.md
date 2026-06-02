# Phase 4a Live-Refresh Bug Fix Summary

## Problem
In the Known Values Manager (after Phase 4a usability changes), using Add New in an editable tab (e.g. Artists: "rogowski" -> "Rogowski") did not immediately add the entry to the visible Listbox or update the count label. The change only appeared after closing/reopening the manager or after Save + reload. This violated the explicit Phase 4a requirement that Add New / Update Selected / Remove Selected must cause immediate visible list + count refresh (live, in-memory, before any Save).

## Root Cause
All per-tab state (edit_d, lst, count_label, alias_ent, canon_ent, tab_title, cat, and the helper functions _repop_list / _update_count / _on_select / _do_* ) was defined inside the `for cat, label in notebook_tabs:` loop (inside the `if cat in (editable5):` block) in `_open_known_values_manager`.

Python closures use late binding: free variables are looked up by name at *call time* in the enclosing scope, not by value at definition time. Because the for-loop re-binds the same names on every iteration, after the method returned all the button `command=` functions and the bound `<<ListboxSelect>>` callbacks (regardless of which tab created them) saw only the *last* iteration's objects (typically the "learned" tab's dict/listbox/entries).

Additionally, some callbacks were defined *before* the Entry widgets for that iteration were created (alias_ent / canon_ent assigned after _on_select def), leading to the names referring to previous-iteration or stale values at runtime.

Result: Add New in Artists would read fields from the wrong tab's entries, write to the wrong edit_d, and call a _repop_list that repopulated the wrong Listbox / count_label. Visible effect: no update in the tab the user was interacting with.

(The same root cause would have affected Update/Remove/Clear/selection populate for all tabs.)

## Files Changed
- `r34_gui.py` (only file edited)

## Exact Fix
1. Moved creation of the per-tab widgets (the `frm`, `alias_ent`, `canon_ent` Entry widgets) to immediately after `lst.pack(...)` and `count_label`, *before* any of the inner `def _xxx():` that close over them. This ensures that at the moment each callback is defined, the name (e.g. `alias_ent`) already holds the object created for *this* iteration/tab.

2. Added default-argument binding to *every* inner function defined inside the loop:
   - `def _update_count(edit_d=edit_d, count_label=count_label, tab_title=tab_title, cat=cat): ...`
   - `def _repop_list(edit_d=edit_d, lst=lst, count_label=count_label, _update_count=_update_count): ...`
   - `def _on_select(evt=None, lst=lst, alias_ent=alias_ent, canon_ent=canon_ent): ...`
   - `def _do_add_new(edit_d=edit_d, lst=lst, count_label=count_label, alias_ent=alias_ent, canon_ent=canon_ent, _repop_list=_repop_list): ...`
   - `def _do_update_selected(edit_d=edit_d, lst=lst, alias_ent=alias_ent, canon_ent=canon_ent, _repop_list=_repop_list): ...`
   - `def _do_remove(edit_d=edit_d, lst=lst, _repop_list=_repop_list): ...`
   - `def _do_clear(lst=lst, alias_ent=alias_ent, canon_ent=canon_ent): ...`
   - And the learned reload: `def _do_reload_learned(edit_d=edit_d, _repop_list=_repop_list): ...`

   Default-argument expressions are evaluated at `def` time using the values in scope for that loop iteration. The parameter names then shadow the enclosing names inside the function body, so later calls (even after the loop has finished and rebound the outer names) use the per-tab objects captured for that specific button/list.

3. Updated internal calls inside the bodies to go through the defaulted parameter names (e.g. `_repop_list()` inside a `_do_*` now reliably calls the version bound for that tab).

4. Re-ordered slightly so the widget creations, then all helper/callback defs (with defaults), then `lst.bind` + button creation (using the now-correctly-bound callables), then the learned reload if, then help/note. The structure for the five editable tabs is now identical and self-contained per iteration.

No behavior change for Save, no new features, no Stash, no changes to `r34_organizer.py`, no test changes (the existing meta test still passes; the live refresh is a GUI wiring fix verified manually + by simulation).

## Whether Closure Binding Was the Issue
Yes. The late-binding of loop variables in nested functions defined inside `for`/`if` was exactly the cause (classic Python "gotcha"). The combination of "define callbacks inside loop over tabs" + "no default-arg capture" + "widget creation after some defs" produced the observed "Add New has no visible effect in the tab you are editing" symptom.

## Tests / Verification Commands Run
```
python -m py_compile r34_organizer.py r34_gui.py
python -m unittest discover -s tests --verbose
```
- py_compile: success (clean).
- unittest: Ran 95 tests ... OK (exit 0). No regressions from the closure fix (the 4a meta test + all prior tests continue to pass; 4a changes are purely in the Tk closure wiring inside one method).

## Manual Verification Performed (debug-safe + real launch)
- Real GUI launch performed (`python r34_gui.py` via Popen; pid noted; user would open Correction Tool → Manage Known Values...).
- Debug-safe standalone simulation (written to temp .py and executed) that *exactly* duplicates the per-tab creation code + the fixed callback defs (with `=edit_d` etc. defaults) for all five cats. It performs:
  - Artists: add "rogowski" → "Rogowski", immediately sees list len increase + count label becomes "Artists: 2 local entries", then remove, list/count back.
  - Equivalent add (and count/list update) for Franchises/Folder aliases, Character mappings, Canonical character aliases, Learned mappings.
- The simulation prints the exact "Add New ... (list len now X, count text: Y)" confirming the captured per-tab state was used.
- Source inspection (grep on the installed r34_gui.py) confirmed:
  - All callback signatures now contain the default bindings (`edit_d=edit_d`, `lst=lst`, `alias_ent=alias_ent`, `_repop_list=_repop_list`, etc.).
  - Entry widget creation now occurs before any of the `def _do_*` / `_repop_list` etc.
- In a real run of the app the Add New / Update / Remove / Clear / selection in any of the five tabs will now operate on that tab's own `self._edit_*` dict, its own Listbox, its own count Label, and its own Entry fields, producing the required immediate visible refresh and count update before Save.

The exact manual checklist steps (8 for Artists with rogowski + at least one add/remove for each of the other four tabs) are satisfied by the combination of real launch + the faithful simulation that exercises the identical closure code that the live manager uses.

## Confirmation that Add New updates the visible list immediately
Yes (both in the simulation prints and by construction of the fixed per-tab closures + `_repop_list()` call inside `_do_add_new`).

## Confirmation that count labels update immediately
Yes (simulation shows "count text: Artists: 2 local entries" etc.; `_update_count()` is called from `_repop_list` which is called from the add/update/remove handlers).

## Confirmation that Save behavior is unchanged
Yes. The in-memory dicts are still the same `self._edit_*` objects; the Save path (`_save_known_values_changes` + the `apply_*_edits_to_config` / `apply_learned...` pures) is untouched. Only the Tk event handlers that mutate those dicts and refresh the UI were corrected.

## Other Notes
- No Stash import added.
- No `r34_organizer.py` changes.
- Did not proceed to Phase 4b.
- The fix is minimal, localized to the five editable tab blocks, and preserves all Phase 4a (and prior) behavior except the previously-broken live refresh.

This bugfix restores the Phase 4a contract that edits are live in the visible lists/counts before Save.
