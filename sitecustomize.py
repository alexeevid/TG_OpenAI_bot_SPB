
"""
sitecustomize.py (v4)
"""
import importlib, sys, traceback

for mod in ("services.missing_impl", "services.patch_ptb_commands"):
    try:
        importlib.import_module(mod)
    except Exception as e:
        print(f"[sitecustomize] Failed to import {{mod}}: {e}", file=sys.stderr)
        traceback.print_exc()
