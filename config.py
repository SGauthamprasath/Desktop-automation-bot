"""
config.py
---------
One place to switch between a local model (Ollama) and the cloud
Anthropic API. Keeping this as a single flag matters for your project
narrative: Phase 2.5's fine-tuned model replaces LOCAL_MODEL here,
and your evaluation chapter can report numbers for both backends by
just changing BACKEND.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# "local" (Ollama, runs on your machine, free, private) or "cloud" (Anthropic API)
BACKEND = os.getenv("AGENT_BACKEND", "local")

# --- local backend settings ---
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b-instruct")
# Ollama's default context window (~2048 tokens) is smaller than a typical
# prompt here, and Ollama silently drops the START of the prompt when it
# overflows - i.e. the system prompt and the task. Always set it explicitly.
# 6144 fits the compact prompts with room to spare and uses less memory than
# 8192 (benchmark runs were being killed for low memory).
LOCAL_NUM_CTX = int(os.getenv("LOCAL_NUM_CTX", "6144"))

# --- cloud backend settings ---
CLOUD_MODEL = os.getenv("CLOUD_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

MAX_RETRIES_ON_BAD_JSON = 2

# Before acting, ask the model for a short plan (steps + "done when") that the
# actor then sees every step. "0" turns it off - useful for comparing.
USE_PLANNER = os.getenv("USE_PLANNER", "1") == "1"

# Phase 3: when the agent says "done", a separate model call checks the
# current screen against the task and DONE WHEN, and sends it back to work if
# it isn't finished. "0" turns it off - for the with/without comparison.
CHECK_DONE = os.getenv("CHECK_DONE", "1") == "1"

# WinUI 3 apps (e.g. Windows 11 Notepad) only render a menu/dropdown flyout
# while the app is the foreground window - invoked in the background, the
# element reports "expanded" but no items ever appear in the UIA tree. When
# True, the executor focuses the target window before invoking anything that
# supports ExpandCollapse. This trades away the no-focus-steal property for
# those actions only; set to "0" to keep the agent strictly in the background.
FOCUS_BEFORE_EXPAND = os.getenv("FOCUS_BEFORE_EXPAND", "1") == "1"
