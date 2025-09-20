
"""
sitecustomize.py (v2)
Loads hotfix modules that (1) provide missing LLM/embedding functions and
(2) auto-register core command handlers for PTB if they were not wired.
"""
import importlib, sys, traceback

for mod in ("services.missing_impl", "services.patch_ptb_commands"):
    try:
        importlib.import_module(mod)
    except Exception as e:
        print(f"[sitecustomize] Failed to import {mod}: {e}", file=sys.stderr)
        traceback.print_exc()
