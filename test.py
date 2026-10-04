'''from llm_client import get_action

fake_state = {
    "window_title": "Test",
    "elements": [
        {"path": [0], "control_type": "Button", "name": "OK",
         "patterns": ["invoke"], "enabled": True, "visible": True,
         "bounding_box": {"left": 0, "top": 0, "right": 50, "bottom": 20}}
    ]
}
action = get_action("Click the OK button", fake_state, [])
print(action)'''

from pywinauto import Desktop
for w in Desktop(backend="uia").windows():
    print(repr(w.window_text()))