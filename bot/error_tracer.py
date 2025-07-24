import logging
try:
    import sentry_sdk
    _SENTRY = True
except Exception:
    _SENTRY = False
_sentry_inited = False
def init_error_tracer(dsn: str | None):
    global _sentry_inited
    if dsn and _SENTRY:
        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0)
        _sentry_inited = True
        logging.info("Sentry initialized")
    else:
        logging.info("Sentry not configured or not installed")
def capture_exception(exc):
    if _sentry_inited:
        sentry_sdk.capture_exception(exc)
    else:
        logging.exception(exc)
