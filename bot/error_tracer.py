
import logging
try:
    import sentry_sdk
except Exception:  # pragma: no cover
    sentry_sdk = None

def init_error_tracer(dsn: str):
    if not dsn or sentry_sdk is None:
        logging.info("Sentry not configured or not installed")
        return
    sentry_sdk.init(dsn=dsn, traces_sample_rate=1.0)
