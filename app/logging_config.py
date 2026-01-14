import logging
import os
import re
from urllib.parse import urlsplit, urlunsplit


_URL_RE = re.compile(r"\bhttps?://[^\s<>'\")\]]+", re.IGNORECASE)


def _mask_url(url: str) -> str:
    """Mask sensitive parts of a URL (query, fragment, long paths)."""
    try:
        parts = urlsplit(url)

        # Keep scheme + netloc and only the last path segment for context
        path = parts.path or ""
        segs = [s for s in path.split("/") if s]

        if not segs:
            masked_path = "/"
        else:
            masked_path = f"/.../{segs[-1]}"

        # Drop query + fragment entirely
        return urlunsplit((parts.scheme, parts.netloc, masked_path, "", ""))
    except Exception:
        return "<url-hidden>"


def mask_urls_in_text(text: str) -> str:
    return _URL_RE.sub(lambda m: _mask_url(m.group(0)), text)


class MaskUrlsFilter(logging.Filter):
    """A logging filter that masks any http(s) URLs inside log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            masked = mask_urls_in_text(msg)
            if masked != msg:
                # Replace message AFTER formatting args to avoid breaking %-formatting
                record.msg = masked
                record.args = ()
        except Exception:
            # Never break logging
            pass
        return True


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Always mask URLs (especially important when LOG_LEVEL=DEBUG).
    logging.getLogger().addFilter(MaskUrlsFilter())
