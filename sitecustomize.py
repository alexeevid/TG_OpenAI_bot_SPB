
"""
sitecustomize.py

Python imports this module automatically on startup (unless run with -S).
We use it to load our hotfix that provides missing functions required by handlers/telegram_core.py.
This avoids editing your existing files.
"""
import importlib
try:
    importlib.import_module("services.missing_impl")
except Exception as e:
    # Keep process running; just print a warning so the bot still starts even if this fails.
    import sys, traceback
    print("[sitecustomize] Failed to import services.missing_impl:", e, file=sys.stderr)
    traceback.print_exc()
