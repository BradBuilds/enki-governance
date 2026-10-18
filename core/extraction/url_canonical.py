"""
URL canonicalization for cross-source dedup (Phase 2 of
the internal news collapse plan).

Same article published by Reuters, BBC, and AP becomes three signals
with three different URLs. Canonicalizing strips the differences that
don't change identity (tracking params, AMP/mobile variants, www
prefix, fragments, scheme case) so the three URLs produce the same
hash and the second/third arrival becomes an alias of the first.

Pure function — no I/O, no logging side effects. Used at signal-insert
time and from the backfill script.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote


# Query parameter names that are tracking/analytics noise — strip on canon.
# Match by lowercase prefix. Keep narrow: only params that demonstrably
# never change article identity. When in doubt, leave the param alone.
_TRACKING_PARAM_PREFIXES = (
    "utm_",          # Google Analytics tracking
    "_hsenc",        # HubSpot
    "_hsmi",         # HubSpot
    "_ga",           # Google Analytics ID
    "_gl",           # Google cross-domain linker
    "mc_cid",        # Mailchimp
    "mc_eid",        # Mailchimp
    "vero_id",       # Vero
    "vero_conv",     # Vero
    "yclid",         # Yandex
    "wickedid",      # Wicked Reports
)
_TRACKING_PARAM_EXACT = frozenset({
    "fbclid",        # Facebook
    "gclid",         # Google Ads
    "gbraid",        # Google enhanced conversions
    "wbraid",        # Google enhanced conversions
    "msclkid",       # Microsoft Ads
    "dclid",         # DoubleClick
    "twclid",        # Twitter
    "li_fat_id",     # LinkedIn
    "ttclid",        # TikTok
    "igshid",        # Instagram share
    "sscid",         # ShareASale
    "ref",           # generic referrer
    "ref_src",       # Twitter share refs
    "ref_url",
    "share",         # generic share param
    "shared_from",
    "from",          # ambiguous, but mostly tracking on news sites
    "src",           # generic source param
    "source",        # generic source param
    "campaign",
    "campaign_id",
    "spm",           # Alibaba
    "from_source",
    "feature",       # YouTube
    "amp",           # AMP toggle (amp=1)
    "output",        # ?output=amp
})

# Subdomain prefixes that don't change content (strip leading host segment).
_HOST_PREFIX_STRIP = ("www.", "m.", "mobile.", "amp.", "www2.", "www3.")

# Path patterns that signal AMP / mobile variant — strip and treat as canonical.
# Order matters: strip the more specific pattern first.
_PATH_AMP_PATTERNS = (
    re.compile(r"\.amp\.html?$", re.IGNORECASE),
    re.compile(r"/amp/?$", re.IGNORECASE),
    re.compile(r"/amp/", re.IGNORECASE),
    re.compile(r"\.amp/?$", re.IGNORECASE),
)


def canonicalize_url(url: Optional[str]) -> Optional[str]:
    """Return the canonical form of `url`, or None if input is unusable.

    Idempotent: canonicalize_url(canonicalize_url(x)) == canonicalize_url(x).
    Returns None for empty / non-http(s) inputs so callers can skip dedup
    on signals without a real article URL.
    """
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if not s:
        return None

    try:
        parts = urlsplit(s)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    # Always normalize http → https for hashing purposes; the protocol
    # version doesn't change content identity for news articles.
    scheme = "https"

    host = (parts.netloc or "").lower()
    if not host:
        return None
    # Strip default ports
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]
    # Strip cosmetic subdomain prefixes
    for prefix in _HOST_PREFIX_STRIP:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break  # only strip one layer

    path = parts.path or "/"
    # Strip AMP path patterns
    for rx in _PATH_AMP_PATTERNS:
        path = rx.sub("/", path)
    # Collapse duplicate slashes, drop trailing slash (except root)
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    # Filter query params: drop tracking, keep the rest, sort for determinism
    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _TRACKING_PARAM_EXACT:
            continue
        if any(kl.startswith(p) for p in _TRACKING_PARAM_PREFIXES):
            continue
        kept.append((kl, v))
    kept.sort()
    query = urlencode(kept, quote_via=quote)

    # Drop fragment entirely — it's a client-side anchor, not content identity
    return urlunsplit((scheme, host, path, query, ""))


def url_hash(url: Optional[str]) -> Optional[str]:
    """16-char SHA-256 of the canonical URL, or None if not canonicalizable.

    Same truncation length as feed_enrichment._headline_hash for consistency.
    """
    canon = canonicalize_url(url)
    if not canon:
        return None
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def extract_signal_url(payload: Optional[dict]) -> Optional[str]:
    """Pull the article URL out of a signal payload, trying common keys.

    RSS connector uses 'link'; HN/Reddit/some others use 'url'. Returns
    the first non-empty match or None.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("link", "url", "permalink", "article_url", "story_url"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None
