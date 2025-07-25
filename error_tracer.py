import logging
try:
    import sentry_sdk
except Exception:
    sentry_sdk = None

def init_error_tracer(dsn: str | None):
    if not dsn or not sentry_sdk:
        logging.info("Sentry not configured or not installed")
        return
    try:
        sentry_sdk.init(dsn=dsn, traces_sample_rate=1.0)
        logging.info("Sentry initialized")
    except Exception as e:
        logging.error("Failed to init sentry: %s", e)
