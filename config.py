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
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b-instruct")

# --- cloud backend settings ---
CLOUD_MODEL = os.getenv("CLOUD_MODEL", "claude-sonnet-4-6")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

MAX_RETRIES_ON_BAD_JSON = 2
