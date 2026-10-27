# Next Steps: Desktop Automation Agent (Local Models)

Suggestions, design corrections and a roadmap for turning the Phase 0/1
prototype into a general desktop automation agent that runs on local models.

---

## 1. Where the project stands

**Phase 0 and Phase 1 run end to end on a local model** (`qwen2.5:7b-instruct`
via Ollama). The Notepad benchmark passes 3/3, twice in a row, and each pass
is checked against Notepad's real state, not just the model saying `done`.

Bugs found and fixed while getting there:

| Problem | Fix |
|---|---|
| Ollama silently cut the prompt to about 2,048 tokens, so the model never saw the system prompt or the task | Native `/api/chat` with `num_ctx` (`LOCAL_NUM_CTX`, default 8192) and a warning when a prompt is truncated |
| Prompt bloated with unused fields and duplicate labels (11.4k chars) | Element compaction, now about 2.5k chars |
| UIA `SetValue` on Windows 11 Notepad only changed what was displayed; the text vanished on the next redraw | `EM_REPLACESEL` for native Edit/RichEdit controls |
| WinUI 3 menus never appear while the app is in the background | Bring the window to the front before opening a menu (`FOCUS_BEFORE_EXPAND`), logged as `uia_pattern+focus` |
| `toggle` crashed on menu items | Fallback to the raw UIA `Toggle()` call |
| The model couldn't tell whether a menu was open or a setting was on | Perception records `toggled` and `expanded` |
| The model wrote the wrong index paths | Model also returns `target_name`; the path is cross-checked against it |
| The prompt's example ("Edit menu") biased the model toward Edit | Neutral example, and `reasoning` comes first in the JSON |
| "Close Tab" got past the denylist | Match on the first word of the control's name |

**Still open:** the Explorer task needs a "select without opening" action;
`runner.py` still counts a pass whenever the model says `done` and doesn't
reset the app between tasks; the cloud backend wasn't retested; the README is
out of date.

---

## 2. Where the current approach goes wrong

### 2.1 The LLM is doing jobs that tools should do
"Open Spotify" shouldn't mean the model clicking through the Start menu one
element at a time. Many basic tasks (opening apps, files, URLs, settings pages,
hotkeys like Ctrl+S) don't need the UI tree at all. Give the model high-level
tools, such as `open_app`, `press_keys`, `type_text`, `click(element)` and
`open_file`, and keep UI navigation for what really needs it. This one change
will do more for the success rate than fine-tuning.

### 2.2 "Never steal focus" can't be a hard rule
WinUI menus won't appear in the background, and many apps need real keyboard
input. Make background-only the preferred mode, not the only one, and record
when focus is taken. That record is already input for the Phase 2 cost model.

### 2.3 Nested index paths are a bad way to name elements
The model kept getting `[3, 0, 0, 2, 0, 0, 0]`-style paths wrong. Number the
visible elements each step (`[12] MenuItem "Word wrap" (toggle, off)`) and
have the model answer with a number. Your code keeps the mapping from numbers
to real elements. This is easier for small models and uses far fewer tokens.

### 2.4 The model plans only one step ahead
A 7B model choosing one action at a time over a 60-element list loses track
of multi-step tasks. Split the work in two:
- A **planner** breaks the task into subgoals ("open Notepad → open View menu
  → enable Word Wrap").
- An **actor** picks the next action for the current subgoal.

The same small model can play both roles with different prompts.

### 2.5 Success means "the model said done"
`set_value` reported success while the text silently vanished. Add a **check
after each action** (did the screen change the way it should?) and a **final
check per task**. Without that, benchmark numbers don't mean much.

### 2.6 The action set is too small
Invoke, toggle and set_value aren't enough for "almost all basic tasks". Also
needed:
- select (lists, tabs, files)
- expand/collapse
- scroll
- keyboard shortcuts
- typing at the cursor
- waiting for something to load
- reading text back
- switching windows

---

## 3. Is Windows UIA enough?

**No, not on its own.** It's the right core, but it needs layers around it.

UIA covers most native Windows apps well: Notepad, Explorer, Settings, Office,
and most WinUI/WPF/Win32 apps. It's weak or blind in a few places:

| Weak spot | What happens | What to do |
|---|---|---|
| Electron apps (VS Code, Discord, Spotify), some Chromium views | Partial or empty tree unless accessibility is switched on | Start them with accessibility enabled, or fall back to vision |
| Games, canvas or custom-drawn apps, image-heavy UIs | Nothing useful in the tree | Vision fallback |
| Java apps | Need the Java Access Bridge | Vision fallback, or enable the bridge |
| Websites | UIA works through the browser, but poorly | A browser tool (Playwright with the page's accessibility tree) |

### Recommended hybrid design
1. **Tools** for launching, shortcuts and files.
2. **UIA** as the main way to see and act.
3. **Vision only when needed:** when the UIA tree is empty or useless, take a
   screenshot, run a detector to find clickable elements, and click by
   coordinates.

---

## 4. Which local models

- **For actions (text only, on top of UIA):** Qwen2.5-7B-Instruct is a
  reasonable base. Also try newer Qwen releases at a similar size if they run
  on your hardware.
- **For the vision fallback:** a small vision-language model, such as
  Qwen2.5-VL-7B or a GUI-focused open model like UI-TARS. Microsoft's
  OmniParser can detect UI elements in screenshots.
- **Hardware:** Ollama currently puts the 7B model about 58% on CPU, which is
  why each step takes 5–25 seconds. Check your GPU memory. Running a 7B text
  model and a vision model together may be too much, so you may need quantized
  or smaller variants.

---

## 5. Fine-tuning

### 5.1 Whether to fine-tune
Probably yes, eventually, but as the **last** step, not the next one. First
fix the setup (tools, numbered elements, planner, checks), then measure which
failures are left. If the remaining errors are things like "picked the wrong
element", "didn't know when it was done" or "broke the JSON format",
fine-tuning fixes those well. It also gives the report a clear before/after
result.

### 5.2 How
- Use **LoRA or QLoRA** on the base model (for example Qwen2.5-7B) with
  **Unsloth** or **LLaMA-Factory**.
- A 7B QLoRA run needs roughly 10–16 GB of GPU memory. If you don't have that,
  free Colab or Kaggle GPUs are enough.
- Export to **GGUF**, load it into Ollama, and switch `LOCAL_MODEL` in
  `config.py`, which was already designed for this.

### 5.3 What data
Each example is *(task + current screen in your exact format + history) →
correct next action*. In order of usefulness:

1. **Your own verified runs, using a stronger model as teacher (best option).**
   - Run a strong cloud model through your own agent (the cloud backend
     already exists) on a few hundred varied tasks across 8–10 apps.
   - Keep only the runs your checks confirm actually succeeded, and turn every
     step into a training example.
   - The local model then learns to imitate the strong one, in exactly the
     input format it will see at run time. This is called distillation, and
     it's a strong final-year contribution in itself.
2. **Generated single-step examples (cheap and plentiful).**
   - Walk the UIA trees of many apps automatically and create examples like
     "click Save", "type 'hello' into the search box" or "turn on Bluetooth".
   - The correct answer is known by construction, so this teaches the model to
     find the right element without any manual labelling.
3. **Ask-vs-act examples for the Phase 2 gate.**
   - Ambiguous tasks, like "click Submit" when there are two, labelled as "ask".
   - Risky actions, like delete or send, labelled as "confirm first".
4. **Public datasets (supplementary only).**
   - Mind2Web for websites, AndroidControl or Android-in-the-Wild for phones,
     OS-Atlas and similar for GUI grounding.
   - Most are screenshots of web or Android, not Windows UIA trees. Use them to
     borrow task ideas and for vision grounding, not as the main data.
   - For evaluation, look at the Windows Agent Arena and OSWorld benchmarks.

### 5.4 How much, and how to test
- A few thousand verified steps is enough for a useful LoRA.
- Always test on **apps the model never saw in training**, not just new tasks
  in familiar apps. Otherwise you're measuring memorization.

---

## 6. Microsoft UFO: the closest existing work

### 6.1 Versions

| Version | Released | What it is |
|---|---|---|
| **UFO** | Feb 2024 | The original. Two agents: a **HostAgent** that picks the right app, and an **AppAgent** that acts inside it. It works from screenshots with the UI's controls labelled on them, plus the UIA control list, and acts through pywinauto. |
| **UFO²** ("Desktop AgentOS") | Apr 2025 | The mature version, now on long-term support. This is the one to study. |
| **UFO³ Galaxy** | Nov 2025 | Coordinates tasks across Windows, Linux and Android devices. Still experimental. |

### 6.2 How UFO² works
- **Planner and app agents.** The HostAgent breaks the task into subtasks and
  hands each one to an AppAgent that specializes in one app.
- **UIA plus vision.** UIA is the main way to find controls. OmniParser-v2 (a
  YOLO detector plus a fine-tuned Florence-2 model) finds the ones UIA misses.
- **GUI actions and app APIs together.** Where an app has its own programming
  interface (Office through COM, for example), it calls that directly instead
  of clicking.
- **Speculative multi-action.** One model call predicts several actions in a
  row. Each is checked against UIA (is the control enabled? visible?) before
  running. They report this cuts model calls by up to 51.5%.
- **Picture-in-Picture desktop.** The agent works in a separate virtual desktop
  session (Remote Desktop loopback), so the user can keep using their own
  desktop while it runs.
- **Knowledge retrieval.** The agent looks up help documents, demonstrations
  and its own past runs while working.
- **Safety.** Actions matching user-defined risk rules (deleting files, closing
  unsaved work, turning on the webcam) pause and ask the user for confirmation.

### 6.3 Results (from the paper)

| Agent | Windows Agent Arena | OSWorld-W |
|---|---|---|
| Original UFO (GPT-4o) | 19.5% | 12.2% |
| OpenAI Operator | 20.8% | 14.3% |
| UFO² (GPT-4o) | 27.9% | 28.6% |
| UFO² (o1) | **30.5%** | **32.7%** |

Even Microsoft's best Windows agent, running on a frontier model, **fails about
70% of benchmark tasks**.

### 6.4 Weak points
1. **It needs cloud models and big hardware.** Every reported result uses
   GPT-4o, o1 or Operator through Azure. The paper doesn't evaluate any open or
   local model, and OmniParser ran on an A100 80 GB GPU. Screenshots of the
   desktop go to the cloud at every step.
2. **Its success rate is still low.** About 30% overall, and only **9.1% on
   tasks that span several apps**.
3. **Finding controls is the main way it fails.** Over 62% of the basic
   version's failures on Windows Agent Arena were controls it couldn't detect,
   mostly where UIA coverage is poor (LibreOffice especially). On OSWorld-W,
   planning mistakes were more common.
4. **Vision isn't a guaranteed fix.** In their own tests, vision alone did
   *worse* than UIA alone on OSWorld-W (14.3% vs 22.4%). Combining both gives
   the best result, but the gains are modest.
5. **Safety is just a list of rules in a prompt.** The model decides whether an
   action matches a risk description. There's no confidence score, no measure
   of how reversible an action is, and no handling of *ambiguous* requests like
   "which Submit button?".
6. **Checking results and recovering from errors are weak.** I didn't find a
   dedicated mechanism for confirming an action's effect or undoing it. A
   precondition check (is the control enabled and visible?) wouldn't catch our
   Notepad `SetValue` bug, because the problem is the effect, not the
   precondition.
7. **It's expensive and slow per task.** Even with speculative execution, it
   takes 6–10 steps per task, each with screenshots and long prompts. That's
   fine on a cloud API, but painful on a local model.
8. **Picture-in-Picture probably won't work on this PC.** It relies on Remote
   Desktop loopback. As far as I know, Windows Home can't host Remote Desktop
   sessions, so that feature likely needs Pro or Enterprise. Worth confirming.

### 6.5 Where this project can do better
Don't try to beat UFO's raw success rate with a 7B model. Beat it on the
problems it leaves open:

1. **Local and private, measured properly.** Show that a small local model,
   set up well and fine-tuned, gets reasonably close to a cloud agent on a
   fixed set of everyday tasks, with no desktop data leaving the machine. UFO
   never measured this.
2. **A real ask-vs-act gate (Phase 2) instead of a rule list.** Decide whether
   to act or ask from the model's **confidence** combined with the **cost of
   the action** (reversible or not, focus-stealing or background, destructive
   or harmless). Measure both kinds of mistake: asking when it didn't need to,
   and acting when it should have asked. Include ambiguous tasks, which UFO
   doesn't handle. **This is the strongest original contribution.**
3. **Check what each action actually did, then recover (Phase 3).** After
   every action, compare the screen before and after against what should have
   changed, and undo or retry when they don't match. The Notepad `SetValue`
   bug, where the action reported success but had no lasting effect, is a
   perfect motivating example for the report.
4. **Text first, vision only when needed.** UFO's own results show UIA alone is
   competitive. On a laptop, call the vision model only when the UIA tree is
   empty or missing what the task needs, and report how much compute that
   saves.
5. **Distill from a teacher.** Use a cloud model to generate verified runs,
   then fine-tune the local model on them. UFO uses large models directly and
   never trains a small one.
6. **Reuse what worked.** Store successful action sequences as reusable
   "skills" and replay them directly, with checks, so repeated tasks don't need
   the model at all.

### 6.6 Practical uses of UFO for this project
- Use its three failure categories (planning, execution, control detection)
  in your own evaluation, so your numbers can be compared with theirs.
- Consider running UFO² as a baseline. Its docs mention support for models
  beyond OpenAI. If it can be pointed at the local Ollama server, "UFO² with
  local Qwen" vs "this system with local Qwen" on the same tasks would make a
  very strong comparison chapter. Not yet checked whether that works.
- Borrow its benchmark ideas, not its whole setup. Windows Agent Arena needs a
  heavy VM setup; a smaller custom task set modelled on its categories is more
  realistic.

---

## 7. Realistic scope

"Do almost any basic task" is still an open research problem; even the best
cloud agents are well below human success rates on Windows and OS benchmarks.
For a final-year project:

- Define a **fixed set of 50–100 tasks** across 8–10 common apps, in
  categories like launch, type, menu, settings, files and browser.
- Make the claim measurable: "X% success, fully local, with a safety gate",
  plus a before/after comparison for fine-tuning.

---

## 8. Suggested order

1. Add the tool layer (launch, shortcuts, typing, window switching) and the
   missing actions (select, scroll, expand, wait).
2. Switch to numbered elements and add a planner.
3. Add per-action and per-task checks to `runner.py`, reset app state between
   tasks, and grow the task set to 50 or more.
4. Build Phase 2 (the ask-vs-act gate) and Phase 3 (recovery).
5. Add the vision fallback for apps where UIA fails.
6. Collect verified runs with the cloud model as teacher, fine-tune, and
   compare the base and fine-tuned models on the same benchmark.

---

## Sources
- [UFO² paper (arXiv 2504.14603)](https://arxiv.org/html/2504.14603v2)
- [UFO² on Microsoft Research](https://www.microsoft.com/en-us/research/publication/ufo2-the-desktop-agentos/)
- [microsoft/UFO on GitHub](https://github.com/microsoft/UFO)
