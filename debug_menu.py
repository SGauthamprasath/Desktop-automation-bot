"""
debug_menu.py
-------------
One-off diagnostic script - NOT part of the agent pipeline.

Answers the open question directly: does opening Notepad's Edit menu
produce new elements perception can see, and does it matter whether we
use invoke() or expand()? Runs entirely within one Python process so
focus is never stolen from Notepad (which is what made manual
before/after snapshots via two separate script runs impossible).

Run with Notepad open on test_scratch.txt, Edit menu CLOSED.
"""

import time
from perception import connect_to_app, get_screen_state, save_snapshot, detect_popup

window = connect_to_app(".*Notepad")

print("=" * 60)
print("STATE 1: before touching the Edit menu")
print("=" * 60)
state_before = get_screen_state(window)
save_snapshot(state_before, label="state1_before")
print(f"Element count: {len(state_before['elements'])}")

# Locate the "Edit" MenuItem directly - per your snapshot, this is path [2, 0, 0, 1]
edit_item = window.children()[2].children()[0].children()[0].children()[1]
assert edit_item.element_info.name == "Edit", f"expected 'Edit', found '{edit_item.element_info.name}'"

print("\n" + "=" * 60)
print("TEST A: edit_item.invoke()")
print("=" * 60)
edit_item.invoke()
time.sleep(0.5)  # let the UI actually render, if it's going to

popup = detect_popup(window)
print(f"Popup detected: {popup.window_text() if popup else None}")
state_after_invoke = get_screen_state(window, popup=popup)
save_snapshot(state_after_invoke, label="state2_after_invoke")
print(f"Element count: {len(state_after_invoke['elements'])}  "
      f"(was {len(state_before['elements'])} before)")

# Close whatever might have opened, cleanly, before testing the other method.
# Escape is a safe way to dismiss a menu without touching anything in it.
from pywinauto.keyboard import send_keys
send_keys('{ESC}')
time.sleep(0.3)

print("\n" + "=" * 60)
print("TEST B: edit_item.expand() [ExpandCollapse pattern]")
print("=" * 60)
if hasattr(edit_item, "expand"):
    edit_item.expand()
    time.sleep(0.5)
    popup = detect_popup(window)
    print(f"Popup detected: {popup.window_text() if popup else None}")
    state_after_expand = get_screen_state(window, popup=popup)
    save_snapshot(state_after_expand, label="state3_after_expand")
    print(f"Element count: {len(state_after_expand['elements'])}  "
          f"(was {len(state_before['elements'])} before)")
else:
    print("This element has no .expand() method available via pywinauto - "
          "would need the raw iface_expand_collapse interface instead.")

send_keys('{ESC}')

print("\n" + "=" * 60)
print("DONE - compare the three saved snapshot files in snapshots/")
print("state1_before / state2_after_invoke / state3_after_expand")
print("=" * 60)
