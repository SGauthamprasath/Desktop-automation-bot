"""Drive the companion bot from Python, using only the standard library.

Start the app first (npm run dev), then:  python examples/agent_client.py

This replays the ask-vs-act idea through the HTTP API, the way a real agent
would: show what it's doing, ask when something is ambiguous, wait for the
user's click, then carry on.
"""

import json
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:7331"  # must match config.http.port


def _request(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"{method} {path} failed: {err.code} {err.read().decode()}") from None


_KEEP = object()  # "don't touch the bubble"


def set_state(state, message=_KEEP, choices=None, **options):
    """POST /state. message: text to show, None to hide the bubble, or leave
    out to keep it as is. options: hold, durationMs, facing. Returns the
    reply, which includes 'questionId' when choices were given."""
    body = {"state": state, **options}
    if message is not _KEEP:
        body["message"] = message  # None becomes JSON null = hide
    if choices is not None:
        body["choices"] = choices
    return _request("POST", "/state", body)


def wait_for_choice(question_id, poll_seconds=25):
    """Long-poll GET /choice until this question is no longer pending.
    Returns the final result: status is answered, dismissed or superseded."""
    while True:
        result = _request("GET", f"/choice?wait={poll_seconds}", timeout=poll_seconds + 5)
        if result["questionId"] != question_id:
            return {**result, "status": "superseded"}
        if result["status"] != "pending":
            return result


if __name__ == "__main__":
    import time

    set_state("thinking", "Looking at the page...")
    time.sleep(2)

    reply = set_state(
        "alert",
        "I found two 'Submit' buttons. Which one did you mean?",
        ["The one in the form", "The one in the popup"],
    )
    print("Asked question", reply["questionId"], "- waiting for a click...")
    result = wait_for_choice(reply["questionId"])
    print("Result:", result)

    if result["status"] == "answered":
        set_state("thinking", None)
        time.sleep(2)
        set_state("talking", f"Done: clicked \"{result['choice']}\".", durationMs=4000)
    else:
        set_state("idle", None)
