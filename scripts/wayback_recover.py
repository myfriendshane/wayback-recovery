#!/usr/bin/env python3
"""
wayback_recover.py — Recover WordPress blog posts and images from the Wayback Machine.

Usage
-----
  python scripts/wayback_recover.py \
      --index-url "https://example.com/" \
      --output-dir ./output \
      --mode dry-run

  python scripts/wayback_recover.py \
      --index-url "https://example.com/" \
      --output-dir ./output \
      --mode full

  # Direct Wayback archive URL (for sites saved via "Save Page Now"):
  python scripts/wayback_recover.py \
      --index-url "https://web.archive.org/web/20251113082400/https://example.com/blog/" \
      --output-dir ./output \
      --mode full

Flags
-----
  --index-url   The original site URL to recover, or a Wayback Machine archive
                URL (https://web.archive.org/web/TIMESTAMP/ORIGINAL_URL).
                Required.
  --output-dir  Directory to write recovered HTML, assets, and WXR (required).
  --mode        dry-run  — query CDX / inspect archive page; no files written.
                full     — download HTML, extract images, write WXR.

Exit codes
----------
  0  Success.
  1  Fatal error (bad arguments, network failure after all retries, etc.).

Requirements
------------
  pip install requests beautifulsoup4 lxml

Notes
-----
  - When --index-url is a Wayback Machine URL the CDX API is not used; post
    links are extracted directly from the archived index page with pagination
    followed automatically.
  - Respects the Wayback Machine's Retry-After header on 429/503 responses.
  - Uses exponential back-off (up to MAX_RETRIES attempts) on transient errors.
  - Only the latest successful (HTTP 200) CDX snapshot is used per URL.
  - In full mode a minimal WXR file (wxr_output.xml) is written to --output-dir.
  - Only images (featured + content) are downloaded, not CSS/JS.
  - You must own or have rights to the content you are recovering.
"""

import argparse
import hashlib
import logging
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = "wayback-recover-bot/1.0 (https://github.com/myfriendshane/wayback-recovery)"
CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"

MAX_RETRIES = 5
BACKOFF_BASE = 2          # seconds; doubles each retry
REQUEST_TIMEOUT = 30      # seconds per HTTP request
INTER_REQUEST_DELAY = 1   # polite pause between requests (seconds)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("wayback_recover")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    """Return a requests Session with the bot User-Agent pre-set."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


SESSION = _session()


def fetch_with_retry(url: str, *, stream: bool = False) -> requests.Response:
    """
    GET *url* with exponential back-off retry.

    Retries on:
      - ConnectionError / Timeout
      - HTTP 429 (Too Many Requests) — honours Retry-After header
      - HTTP 503 (Service Unavailable)   — honours Retry-After header

    Raises requests.HTTPError on non-retryable 4xx/5xx after exhausting retries.
    """
    delay = BACKOFF_BASE
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT, stream=stream)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning("Network error on attempt %d/%d for %s: %s — retrying in %ds",
                        attempt, MAX_RETRIES, url, exc, delay)
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code in (429, 503):
            raw_retry = resp.headers.get("Retry-After", "")
            try:
                retry_after = int(raw_retry)
            except (ValueError, TypeError):
                # Retry-After may be an HTTP-date; fall back to current delay
                retry_after = delay
            log.warning("HTTP %d on attempt %d/%d for %s — waiting %ds",
                        resp.status_code, attempt, MAX_RETRIES, url, retry_after)
            resp.close()  # release connection before sleeping
            if attempt == MAX_RETRIES:
                resp.raise_for_status()
            time.sleep(retry_after)
            delay = retry_after * 2
            continue

        resp.raise_for_status()
        return resp

    # Should never reach here, but satisfy type checkers.
    raise RuntimeError("fetch_with_retry exhausted without returning")


# ---------------------------------------------------------------------------
# CDX — discover snapshots
# ---------------------------------------------------------------------------

# URL path fragments that indicate non-post pages to exclude.
_EXCLUDE_PATH_FRAGMENTS = (
    "/product/", "/shop/", "/cart/", "/checkout/", "/my-account/",
    "/wp-admin/", "/wp-content/", "/wp-includes/", "/wp-json/",
    "/feed/", "/author/", "/category/", "/tag/", "/page/",
    "/search/", "/comments/", "/trackback/",
    "/info/", "/about/", "/contact/", "/privacy/", "/terms/",
)


def should_process_url(url: str) -> tuple[bool, str]:
    """
    Decide whether *url* looks like a blog post worth recovering.

    Returns ``(True, "")`` if the URL should be processed, or
    ``(False, reason)`` with a short human-readable reason string if it
    should be skipped.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not parse URL %r: %s — skipping", url, exc)
        return False, "unparseable URL"

    path = parsed.path.lower()

    # Reject URLs with query strings — not clean post permalinks.
    if parsed.query:
        return False, "has query string"

    for fragment in _EXCLUDE_PATH_FRAGMENTS:
        if fragment in path:
            return False, f"excluded path fragment '{fragment}'"

    # A valid post permalink ends with a slug segment (non-empty path).
    # Reject bare root or paths with no real slug.
    clean = path.strip("/")
    if not clean:
        return False, "root URL"

    # Reject paths whose last segment ends with a known non-post file extension.
    # Using an explicit allowlist of extensions avoids false positives on
    # slugs that legitimately contain dots (e.g. /about-v1.5-release/).
    _NON_POST_EXTENSIONS = (
        ".php", ".xml", ".txt", ".json", ".rss", ".atom",
        ".css", ".js", ".html", ".htm",
    )
    last_segment = clean.rsplit("/", 1)[-1]
    for ext in _NON_POST_EXTENSIONS:
        if last_segment.endswith(ext):
            return False, f"path has non-post file extension ('{last_segment}')"

    return True, ""


def parse_wayback_url(url: str) -> tuple[str | None, str | None]:
    """
    Parse a Wayback Machine URL.

    Returns ``(timestamp, original_url)`` if *url* is a Wayback URL,
    otherwise ``(None, None)``.

    Supports optional modifiers (``if_``, ``id_``, ``js_``, ``cs_``, ``im_``)
    between the timestamp and the original URL.

    Example::

        parse_wayback_url(
            'https://web.archive.org/web/20251113082400/'
            'https://www.currylovers.co.za/curry-school/'
        )
        # -> ('20251113082400', 'https://www.currylovers.co.za/curry-school/')
    """
    pattern = r'https?://web\.archive\.org/web/(\d{14})(?:id_|if_|js_|cs_|im_)?/(https?://.+)$'
    match = re.match(pattern, url)
    if match:
        return (match.group(1), match.group(2))
    return (None, None)


def extract_original_url_from_wayback(url: str) -> str:
    """
    Extract the original URL from a Wayback-wrapped URL.

    Handles any modifier suffix (``im_``, ``if_``, ``id_``, etc.) and
    timestamps with 1–14 digits.  Returns *url* unchanged if it is not a
    Wayback URL.

    Examples::

        extract_original_url_from_wayback(
            'https://web.archive.org/web/20251113082400im_/'
            'https://www.currylovers.co.za/image.jpg'
        )
        # -> 'https://www.currylovers.co.za/image.jpg'

        extract_original_url_from_wayback('https://www.currylovers.co.za/image.jpg')
        # -> 'https://www.currylovers.co.za/image.jpg'  (unchanged)
    """
    match = _WAYBACK_UNWRAP_RE.match(url)
    if match:
        return match.group(1)
    return url


def _resolve_wayback_href(href: str, base_ts: str | None, base_orig: str | None, base_url: str) -> str:
    """
    Resolve an href found on a Wayback-archived page to an absolute URL.

    Handles three cases:

    1. Already an absolute Wayback URL (``https://web.archive.org/web/...``).
    2. A root-relative Wayback path (``/web/TIMESTAMP/...``) — Wayback Machine
       commonly emits these instead of fully-qualified URLs.
    3. Any other relative href — resolved against the original URL and then
       wrapped in a Wayback URL when *base_ts* / *base_orig* are available,
       otherwise resolved against *base_url* directly.

    Args:
        href:      The raw ``href`` value from the HTML anchor element.
        base_ts:   Wayback timestamp extracted from the page's own URL, or
                   ``None`` if the page URL is not a Wayback URL.
        base_orig: Original (non-Wayback) URL of the archive page, or ``None``.
        base_url:  Fallback absolute URL used for plain relative hrefs when
                   *base_ts* / *base_orig* are not available.
    """
    # Case 1: already a full Wayback absolute URL.
    if href.startswith("https://web.archive.org/web/"):
        return href

    # Case 2: root-relative Wayback path like /web/20251113082400/https://...
    if href.startswith("/web/"):
        return "https://web.archive.org" + href

    # Case 3: relative href — resolve against the original URL and wrap.
    if base_ts and base_orig:
        original_absolute = urllib.parse.urljoin(base_orig, href)
        return f"{WAYBACK_BASE}/{base_ts}/{original_absolute}"

    return urllib.parse.urljoin(base_url, href)


def extract_post_links(html: str, base_url: str) -> list[str]:
    """
    Extract blog post URLs from an archive page's HTML.

    Finds all ``<article>`` tags and extracts the permalink ``<a rel="bookmark">``
    inside each.  Falls back to ``<h2 class="entry-title"> > <a>`` when no
    bookmark link is present (e.g. Elementor-built archive pages).  Filters
    results using :func:`should_process_url` to exclude non-post URLs.  Handles
    absolute Wayback hrefs, root-relative Wayback paths
    (``/web/TIMESTAMP/...``), and plain relative hrefs.
    """
    soup = BeautifulSoup(html, "lxml")
    post_links: list[str] = []
    seen: set[str] = set()

    # If base_url is a Wayback URL, extract the timestamp + original for
    # properly wrapping any relative hrefs that haven't been rewritten.
    base_ts, base_orig = parse_wayback_url(base_url)

    articles = soup.find_all("article")
    log.debug("Found %d <article> tag(s) on page", len(articles))

    for article in articles:
        link_tag = article.find("a", rel="bookmark")
        if not link_tag:
            # Fallback: Elementor and some themes use <h2 class="entry-title"><a>
            h2 = article.find("h2", class_=_ENTRY_TITLE_RE)
            if h2:
                link_tag = h2.find("a")
            if not link_tag:
                log.debug("Article has no recognisable permalink link — skipping")
                continue

        href = link_tag.get("href", "")
        if not href:
            continue

        absolute = _resolve_wayback_href(href, base_ts, base_orig, base_url)
        log.debug("Candidate post link: %s", absolute)

        if absolute in seen:
            continue

        # For Wayback-rewritten links, evaluate the original URL for filtering.
        ts, orig = parse_wayback_url(absolute)
        url_to_check = orig if orig else absolute
        include, reason = should_process_url(url_to_check)
        if not include:
            log.debug("Skipping post link %s — %s", absolute, reason)
            continue

        seen.add(absolute)
        post_links.append(absolute)
        log.debug("Accepted post: %s", url_to_check)

    return post_links


def extract_pagination_links(html: str, current_page_url: str) -> list[str]:
    """
    Extract pagination URLs from an archive page's HTML.

    Looks for a ``<nav>`` element whose ``class`` attribute includes
    ``"pagination"`` and collects all ``<a class="page-numbers">`` hrefs,
    excluding the current page.  Handles absolute Wayback hrefs, root-relative
    Wayback paths (``/web/TIMESTAMP/...``), and plain relative hrefs.
    Returns a deduplicated list in document order.
    """
    soup = BeautifulSoup(html, "lxml")
    nav = soup.find("nav", class_="pagination")
    if not nav:
        return []

    # If current_page_url is a Wayback URL, extract the timestamp + original
    # for properly wrapping any relative hrefs.
    base_ts, base_orig = parse_wayback_url(current_page_url)

    pagination_urls: list[str] = []
    seen: set[str] = set()

    for link in nav.find_all("a", class_="page-numbers"):
        href = link.get("href", "")
        if not href:
            continue

        absolute = _resolve_wayback_href(href, base_ts, base_orig, current_page_url)

        if absolute in seen or absolute == current_page_url:
            continue
        seen.add(absolute)
        pagination_urls.append(absolute)

    return pagination_urls


def _discover_wayback_posts(index_url: str) -> tuple[list[dict] | None, str | None]:
    """
    Discover blog post records from a direct Wayback Machine archive URL.

    Fetches the archive page, extracts post links across all paginated pages,
    and returns ``(records, site_url)`` where *records* is a list of
    ``{"timestamp": str, "original": str}`` dicts ready for the download loop
    and *site_url* is the original (non-Wayback) site URL.

    Returns ``(None, None)`` if *index_url* is not a Wayback URL or if the
    archive page cannot be fetched.
    """
    timestamp, original_url = parse_wayback_url(index_url)
    if not timestamp or not original_url:
        return None, None

    log.info("Detected Wayback archive URL")
    log.info("Timestamp: %s", timestamp)
    log.info("Original URL: %s", original_url)

    archive_html = fetch_html(timestamp, original_url)
    if not archive_html:
        log.error("Failed to fetch archive page")
        return None, None

    post_links = extract_post_links(archive_html, index_url)
    post_links_seen: set[str] = set(post_links)
    log.info("Found %d posts on page 1", len(post_links))

    pagination_urls = extract_pagination_links(archive_html, index_url)
    log.info("Found %d additional page(s)", len(pagination_urls))

    for page_num, page_url in enumerate(pagination_urls, 2):
        log.info("Fetching page %d: %s", page_num, page_url)
        page_ts, page_orig = parse_wayback_url(page_url)
        if not page_ts or not page_orig:
            log.warning("Could not parse pagination URL: %s — skipping", page_url)
            continue
        page_html = fetch_html(page_ts, page_orig)
        if page_html:
            page_posts = extract_post_links(page_html, page_url)
            log.info("Found %d posts on page %d", len(page_posts), page_num)
            for link in page_posts:
                if link not in post_links_seen:
                    post_links_seen.add(link)
                    post_links.append(link)

    log.info("Total posts found across all pages: %d", len(post_links))

    records: list[dict] = []
    for link in post_links:
        post_ts, post_orig = parse_wayback_url(link)
        if post_ts and post_orig:
            records.append({"timestamp": post_ts, "original": post_orig})
        else:
            log.warning("Skipping unparseable post link: %s", link)

    return records, original_url


def query_cdx(index_url: str) -> list[dict]:
    """
    Query the CDX API for all HTTP-200 HTML snapshots of *index_url* and its sub-pages.

    Returns a list of dicts with keys: timestamp, original, mimetype, statuscode,
    digest, length.  Only the most recent snapshot per URL is kept (sort=reverse
    ensures collapse=urlkey retains the latest capture for each URL).

    URLs that do not look like blog posts are filtered out before returning.
    """
    params = [
        ("url",       index_url.rstrip("/") + "/*"),
        ("matchType", "prefix"),
        ("output",    "json"),
        ("filter",    "statuscode:200"),
        ("filter",    "mimetype:text/html"),  # skip images/CSS/JS from CDX results
        ("collapse",  "urlkey"),              # one result per unique URL …
        ("sort",      "reverse"),             # … keeping the most recent timestamp
        ("fl",        "timestamp,original,mimetype,statuscode,digest,length"),
    ]
    log.info("Querying CDX API for: %s", index_url)
    resp = fetch_with_retry(CDX_API + "?" + urllib.parse.urlencode(params))
    time.sleep(INTER_REQUEST_DELAY)

    rows = resp.json()
    if not rows:
        log.warning("CDX returned no results for %s", index_url)
        return []

    # First row is the header when output=json
    header = rows[0]
    records = [dict(zip(header, row)) for row in rows[1:]]
    total = len(records)
    log.info("CDX returned %d URLs", total)

    # Filter for blog posts only and collect exclusion stats.
    log.info("Filtering URLs for blog posts only...")
    kept: list[dict] = []
    exclusion_counts: dict[str, int] = {}
    for rec in records:
        include, reason = should_process_url(rec["original"])
        if include:
            kept.append(rec)
        else:
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
            log.debug("Excluded %s — %s", rec["original"], reason)

    excluded = total - len(kept)
    if excluded:
        parts = ", ".join(f"{r}: {c}" for r, c in sorted(exclusion_counts.items()))
        log.info("Excluded %d URLs (%s)", excluded, parts)
    log.info("Processing %d blog posts", len(kept))
    return kept


# ---------------------------------------------------------------------------
# HTML download and image extraction
# ---------------------------------------------------------------------------

# Only images (featured + content) are downloaded, not CSS/JS.

# Image URL fragments that indicate WordPress theme / UI images to skip.
# Directory-based: matched against the full URL path.
_SKIP_IMAGE_DIR_FRAGMENTS = (
    "/wp-content/themes/",
    "/wp-includes/",
)

# Pre-compiled regex for matching the entry-title heading class used by many
# WordPress themes and Elementor as the fallback permalink source.
_ENTRY_TITLE_RE = re.compile(r"entry-title")

# Pre-compiled pattern for stripping the Wayback Machine prefix from a URL.
# Matches the scheme, host, /web/, timestamp, optional modifier (im_, if_, etc.)
# and captures the original URL.
_WAYBACK_UNWRAP_RE = re.compile(
    r'https?://web\.archive\.org/web/\d{1,14}[a-z_]*/(https?://.+)$'
)

# Name-based: matched only against the image filename (last path component),
# to avoid false positives on upload paths like /uploads/logo-design-tips.jpg.
_SKIP_IMAGE_NAME_FRAGMENTS = (
    "logo",
    "icon",
    "sprite",
    "button",
    "avatar",
    "badge",
)


def wayback_url(timestamp: str, original: str) -> str:
    """Construct a Wayback Machine URL that serves the raw archived resource."""
    return f"{WAYBACK_BASE}/{timestamp}if_/{original}"


def fetch_html(timestamp: str, original: str) -> str | None:
    """Download archived HTML for a single snapshot.  Returns None on failure."""
    url = wayback_url(timestamp, original)
    try:
        resp = fetch_with_retry(url)
        time.sleep(INTER_REQUEST_DELAY)
        return resp.text
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to fetch HTML for %s@%s: %s", original, timestamp, exc)
        return None


def extract_images(html: str, base_url: str) -> dict:
    """
    Parse *html* and return a dict with keys:

    - ``"featured"``: absolute URL of the featured image, or ``None``.
    - ``"content"``: deduplicated list of absolute content-image URLs.

    Only ``<img>`` tags are considered (no ``<link>`` or ``<script>``).
    Theme images, WordPress UI images, data: URIs, and off-host images are
    excluded.  Detailed counts are logged.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to parse HTML from %s: %s", base_url, exc)
        return {"featured": None, "content": []}

    try:
        parsed_base = urllib.parse.urlparse(base_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not parse base URL %r: %s", base_url, exc)
        return {"featured": None, "content": []}

    # base_url is the original post URL; the HTML was fetched from the Wayback
    # Machine, so image URLs in the HTML are typically Wayback-wrapped (e.g.
    # https://web.archive.org/web/20251113082400im_/https://example.com/img.jpg).
    # We unwrap before host-checking, but store the original URL so that
    # download_asset() can correctly construct its own Wayback fetch URL.
    base_netloc = parsed_base.netloc

    # --- Featured image detection ---
    featured_img: str | None = None
    featured_method: str = ""

    try:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            featured_img = urllib.parse.urljoin(base_url, og["content"])
            featured_method = "og:image"
    except Exception as exc:  # noqa: BLE001
        log.warning("Error reading og:image from %s: %s", base_url, exc)

    if not featured_img:
        try:
            wp_thumb = soup.find(
                "img",
                class_=re.compile(r"wp-post-image|attachment-post-thumbnail"),
            )
            if wp_thumb and wp_thumb.get("src"):
                featured_img = urllib.parse.urljoin(base_url, wp_thumb["src"])
                featured_method = "wp-post-image class"
        except Exception as exc:  # noqa: BLE001
            log.warning("Error reading wp-post-image from %s: %s", base_url, exc)

    if not featured_img:
        try:
            tw = soup.find("meta", attrs={"name": "twitter:image"})
            if tw and tw.get("content"):
                featured_img = urllib.parse.urljoin(base_url, tw["content"])
                featured_method = "twitter:image"
        except Exception as exc:  # noqa: BLE001
            log.warning("Error reading twitter:image from %s: %s", base_url, exc)

    if featured_img:
        # Apply the same basic safety checks as content images: reject data:
        # URIs and off-host URLs so run_full() never tries to download them.
        if featured_img.startswith("data:"):
            log.warning("Featured image is a data: URI — ignoring (%s)", base_url)
            featured_img = None
            featured_method = ""
        else:
            try:
                # Unwrap Wayback URL to get the original image URL for host check.
                featured_original = extract_original_url_from_wayback(featured_img)
                feat_parsed = urllib.parse.urlparse(featured_original)
                if feat_parsed.netloc and feat_parsed.netloc != base_netloc:
                    log.warning(
                        "Featured image is from a different domain (%s) — ignoring: %s",
                        feat_parsed.netloc, featured_original,
                    )
                    featured_img = None
                    featured_method = ""
                else:
                    # Store original URL so download_asset wraps it correctly.
                    featured_img = featured_original
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not validate featured image URL %r: %s", featured_img, exc)
                featured_img = None
                featured_method = ""

    if featured_img:
        log.info("Found featured image via %s: %s", featured_method, featured_img)
    else:
        log.debug("No featured image found for %s", base_url)

    # --- Content image extraction ---
    content_imgs: list[str] = []
    seen: set[str] = set()
    img_tags = soup.find_all("img")
    total_img_tags = len(img_tags)
    filtered_count = 0

    log.debug("Scanning %d <img> tag(s) for content images", total_img_tags)

    for img in img_tags:
        try:
            raw = img.get("src", "")
            if not raw or raw.startswith("data:"):
                log.debug("Skipping empty/data: src")
                filtered_count += 1
                continue

            absolute = urllib.parse.urljoin(base_url, raw)

            # Unwrap Wayback-wrapped URLs before host and path checks; the
            # archived HTML rewrites every src to a Wayback URL, so comparing
            # against base_netloc would always fail without unwrapping.
            original_url = extract_original_url_from_wayback(absolute)
            parsed = urllib.parse.urlparse(original_url)

            log.debug("Image src: %s  ->  original: %s", absolute, original_url)

            # Only keep images on the same host as the post.
            if parsed.netloc != base_netloc:
                log.debug("Skipping off-host image: %s (host: %s)", original_url, parsed.netloc)
                filtered_count += 1
                continue

            # Skip theme/WordPress UI images (directory-based check on the
            # original URL path so Wayback prefix doesn't interfere).
            orig_lower = original_url.lower()
            if any(frag in orig_lower for frag in _SKIP_IMAGE_DIR_FRAGMENTS):
                log.debug("Skipping theme/includes image: %s", original_url)
                filtered_count += 1
                continue

            # Skip common UI image names only for images that are NOT in the
            # uploads directory — content photos live in /wp-content/uploads/
            # and should never be filtered by name (e.g. logo-design-tips.jpg).
            if "/wp-content/uploads/" not in orig_lower:
                filename_lower = orig_lower.rsplit("/", 1)[-1]
                if any(frag in filename_lower for frag in _SKIP_IMAGE_NAME_FRAGMENTS):
                    log.debug("Skipping UI image by name: %s", original_url)
                    filtered_count += 1
                    continue

            # Deduplicate using the original URL.
            if original_url in seen:
                continue
            seen.add(original_url)

            # Don't duplicate the featured image in the content list.
            if original_url != featured_img:
                log.debug("Accepted content image: %s", original_url)
                content_imgs.append(original_url)

        except Exception as exc:  # noqa: BLE001
            log.warning("Error processing <img> tag from %s: %s", base_url, exc)
            filtered_count += 1

    log.info(
        "Found %d content image(s) (scanned %d <img> tags, filtered %d)",
        len(content_imgs), total_img_tags, filtered_count,
    )
    return {"featured": featured_img, "content": content_imgs}


# ---------------------------------------------------------------------------
# Asset download
# ---------------------------------------------------------------------------

def _safe_filename(url: str) -> str:
    """
    Derive a safe relative file path from an asset URL.

    Path components are split and filtered to strip ``..`` and ``.`` entries,
    preventing directory traversal even with double-encoded sequences
    (e.g. ``%252e%252e``).  The hard security boundary is the ``relative_to``
    check in :func:`download_asset`.
    """
    parsed = urllib.parse.urlparse(url)
    # Decode percent-encoding once, then split into components and drop traversal
    clean_path = urllib.parse.unquote(parsed.path)
    parts = [p for p in clean_path.split("/") if p and p not in (".", "..")]
    rel = "/".join(parts)
    if not rel:
        rel = "index.html"
    return rel


def download_asset(asset_url: str, assets_dir: Path, timestamp: str) -> str | None:
    """
    Download *asset_url* from the Wayback Machine (using *timestamp*) into
    *assets_dir*, preserving relative directory structure.

    Returns the local relative path on success, None on failure.

    Path traversal attempts (e.g. ``../../etc/passwd``) are detected and
    rejected: the resolved destination must be inside *assets_dir*.
    """
    wb_url = wayback_url(timestamp, asset_url)
    rel_path = _safe_filename(asset_url)
    dest = (assets_dir / rel_path).resolve()

    # Security check: dest must be inside assets_dir
    try:
        dest.relative_to(assets_dir.resolve())
    except ValueError:
        log.warning("Skipping asset with unsafe path (traversal detected): %s", asset_url)
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        log.debug("Asset already downloaded: %s", rel_path)
        return rel_path

    try:
        resp = fetch_with_retry(wb_url, stream=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
        time.sleep(INTER_REQUEST_DELAY)
        log.info("Downloaded asset: %s", rel_path)
        return rel_path
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to download asset %s: %s", asset_url, exc)
        return None


# ---------------------------------------------------------------------------
# HTML post-processing — rewrite asset URLs to local relative paths
# ---------------------------------------------------------------------------

def rewrite_asset_urls(html: str, asset_map: dict[str, str]) -> str:
    """
    Rewrite image ``src`` attribute values in *html* using BeautifulSoup.

    Only ``<img src>`` attributes are updated, avoiding accidental replacement
    of text content or unrelated links.
    *asset_map* maps absolute original URLs to local relative paths; both the
    absolute form and the root-relative path form are matched.  Wayback-wrapped
    src values (``https://web.archive.org/web/TIMESTAMP.../URL``) are unwrapped
    to their original URL before lookup, so the rewriting works even when the
    archived HTML has Wayback-rewritten image srcs.
    """
    if not asset_map:
        return html

    # Build a fast lookup covering absolute URLs and their root-relative paths
    lookup: dict[str, str] = {}
    for original_url, local_path in asset_map.items():
        lookup[original_url] = local_path
        parsed = urllib.parse.urlparse(original_url)
        if parsed.path and parsed.path != "/":
            # Only add the path form if it doesn't collide with another entry
            lookup.setdefault(parsed.path, local_path)

    soup = BeautifulSoup(html, "lxml")
    for element in soup.find_all("img"):
        val = element.get("src", "")
        if not val:
            continue
        if val in lookup:
            element["src"] = lookup[val]
            continue
        # src may be a Wayback-wrapped URL; try the unwrapped original.
        # Only attempt the second lookup when the URL actually changed (i.e.
        # it was Wayback-wrapped), to avoid a redundant dict lookup for plain
        # URLs that already missed the first check.
        orig_val = extract_original_url_from_wayback(val)
        if orig_val != val and orig_val in lookup:
            element["src"] = lookup[orig_val]

    return str(soup)


# ---------------------------------------------------------------------------
# WXR (WordPress eXtended RSS) export
# ---------------------------------------------------------------------------

WXR_NAMESPACE = "http://wordpress.org/export/1.2/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"


def build_wxr(posts: list[dict], site_url: str) -> ET.Element:
    """
    Build a minimal WXR ElementTree from *posts*.

    Each post dict must have: title, link, pub_date, content, guid.
    """
    ET.register_namespace("wp", WXR_NAMESPACE)
    ET.register_namespace("content", CONTENT_NS)
    ET.register_namespace("dc", DC_NS)

    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:wp": WXR_NAMESPACE,
        "xmlns:content": CONTENT_NS,
        "xmlns:dc": DC_NS,
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = site_url
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = "Recovered via wayback-recover-bot"
    ET.SubElement(channel, "{%s}wxr_version" % WXR_NAMESPACE).text = "1.2"

    for post in posts:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = post.get("title", "(untitled)")
        ET.SubElement(item, "link").text = post.get("link", "")
        ET.SubElement(item, "pubDate").text = post.get("pub_date", "")
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = post.get("guid", post.get("link", ""))
        content_el = ET.SubElement(item, "{%s}encoded" % CONTENT_NS)
        content_el.text = post.get("content", "")
        ET.SubElement(item, "{%s}post_type" % WXR_NAMESPACE).text = "post"
        ET.SubElement(item, "{%s}status" % WXR_NAMESPACE).text = "publish"

    return rss


def write_wxr(posts: list[dict], site_url: str, output_dir: Path) -> None:
    """Write *posts* as a WXR XML file to *output_dir*/wxr_output.xml."""
    tree = build_wxr(posts, site_url)
    dest = output_dir / "wxr_output.xml"
    ET.indent(tree, space="  ")
    ET.ElementTree(tree).write(str(dest), encoding="unicode", xml_declaration=True)
    log.info("WXR written to %s (%d posts)", dest, len(posts))


# ---------------------------------------------------------------------------
# Post metadata extraction helpers
# ---------------------------------------------------------------------------

def extract_post_title(soup: BeautifulSoup) -> str:
    """Best-effort extraction of a post title from parsed HTML."""
    for selector in ("h1.entry-title", "h1.post-title", "h1", "title"):
        el = soup.select_one(selector)
        if el:
            return el.get_text(strip=True)
    return "(untitled)"


def extract_post_content(soup: BeautifulSoup) -> str:
    """Best-effort extraction of the main post body from parsed HTML."""
    for selector in (".entry-content", ".post-content", "article", "main"):
        el = soup.select_one(selector)
        if el:
            return str(el)
    return str(soup.body) if soup.body else ""


def extract_pub_date(soup: BeautifulSoup) -> str:
    """Best-effort extraction of the publication date."""
    for selector in ("time[datetime]", ".entry-date", ".post-date"):
        el = soup.select_one(selector)
        if el:
            return el.get("datetime", "") or el.get_text(strip=True)
    return ""


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run_dry(index_url: str) -> int:
    """Perform discovery only; print snapshot/post list; return exit code."""
    records, site_url = _discover_wayback_posts(index_url)

    if records is not None:
        if not records:
            log.error("No post links found — nothing to recover.")
            return 1

        assert site_url is not None
        print(f"\nFound {len(records)} post(s) from {site_url}:\n")
        for i, rec in enumerate(records[:50], 1):
            print(f"  [{i}] {rec['original']}")
        if len(records) > 50:
            print(f"  … and {len(records) - 50} more.")
        print()
        return 0

    # Not a Wayback URL — use CDX API.
    records = query_cdx(index_url)
    if not records:
        log.error("No snapshots found — nothing to recover.")
        return 1

    print(f"\nFound {len(records)} snapshot(s) for {index_url}:\n")
    for rec in records[:50]:       # limit console output
        print(f"  [{rec['timestamp']}] {rec['original']}")
    if len(records) > 50:
        print(f"  … and {len(records) - 50} more.")
    print()
    return 0


def run_full(index_url: str, output_dir: Path) -> int:
    """Full recovery: download HTML, extract images, rewrite URLs, write WXR."""
    output_dir.mkdir(parents=True, exist_ok=True)
    html_dir = output_dir / "html"
    assets_dir = output_dir / "assets"
    html_dir.mkdir(exist_ok=True)
    assets_dir.mkdir(exist_ok=True)

    posts: list[dict] = []
    failures: list[str] = []
    failed_images: list[str] = []

    # Running image statistics across all posts.
    total_featured_found = 0
    total_featured_downloaded = 0
    total_content_found = 0
    total_content_downloaded = 0

    # ------------------------------------------------------------------
    # Determine the list of (timestamp, original_url) pairs to process.
    # ------------------------------------------------------------------
    records, site_url = _discover_wayback_posts(index_url)

    if records is not None:
        if not records:
            log.error("No post links found — nothing to recover.")
            return 1
    else:
        # Not a Wayback URL — use CDX API (existing flow).
        records = query_cdx(index_url)
        if not records:
            log.error("No snapshots found — nothing to recover.")
            return 1
        site_url = index_url

    for i, rec in enumerate(records, 1):
        original = rec["original"]
        timestamp = rec["timestamp"]
        log.info("[%d/%d] Processing: %s", i, len(records), original)

        html = fetch_html(timestamp, original)
        if html is None:
            failures.append(original)
            continue

        # --- image extraction and download ---
        try:
            images = extract_images(html, original)
        except Exception as exc:  # noqa: BLE001
            log.warning("Image extraction failed for %s: %s — skipping images", original, exc)
            images = {"featured": None, "content": []}

        asset_map: dict[str, str] = {}

        # Download featured image first.
        post_featured_downloaded = 0
        if images["featured"]:
            total_featured_found += 1
            log.info("Downloading featured image...")
            local_path = download_asset(images["featured"], assets_dir, timestamp)
            if local_path:
                asset_map[images["featured"]] = local_path
                post_featured_downloaded = 1
                total_featured_downloaded += 1
                log.info("Downloaded featured image: %s", local_path)
            else:
                log.warning("Failed to download featured image: %s", images["featured"])
                failed_images.append(images["featured"])

        # Download content images.
        content_urls = images["content"]
        post_content_found = len(content_urls)
        total_content_found += post_content_found
        post_content_downloaded = 0

        if content_urls:
            log.info("Downloading %d content image(s)...", post_content_found)
            for img_url in content_urls:
                local_path = download_asset(img_url, assets_dir, timestamp)
                if local_path:
                    asset_map[img_url] = local_path
                    post_content_downloaded += 1
                    total_content_downloaded += 1
                else:
                    failed_images.append(img_url)

        post_featured_found = 1 if images["featured"] else 0
        log.info(
            "Post %d: found %d featured + %d content image(s), downloaded %d/%d",
            i,
            post_featured_found,
            post_content_found,
            post_featured_downloaded + post_content_downloaded,
            post_featured_found + post_content_found,
        )

        # Rewrite image URLs in the full page HTML (attribute-safe, via BS4)
        html_rewritten = rewrite_asset_urls(html, asset_map)

        # Extract post metadata and content from the rewritten HTML
        soup_rewritten = BeautifulSoup(html_rewritten, "lxml")
        title = extract_post_title(soup_rewritten)
        content_rewritten = extract_post_content(soup_rewritten)
        pub_date = extract_pub_date(soup_rewritten)

        # --- save rewritten HTML to disk ---
        # Include a short URL hash to avoid collisions between URLs that share
        # the same final path segment (e.g. /blog/post/ vs /news/post/).
        url_hash = hashlib.sha256(original.encode()).hexdigest()[:8]
        safe_name = re.sub(r"[^\w\-]", "_", original.rstrip("/").split("/")[-1] or "index")
        safe_name = safe_name.rstrip(".")  # remove any trailing dots
        html_file = html_dir / f"{timestamp}_{safe_name}_{url_hash}.html"
        html_file.write_text(html_rewritten, encoding="utf-8")

        posts.append({
            "title": title,
            "link": original,
            "pub_date": pub_date,
            "content": content_rewritten,
            "guid": original,
        })

    # --- WXR export ---
    if posts:
        write_wxr(posts, site_url, output_dir)
    else:
        log.error("No posts were successfully recovered.")

    if failures:
        log.warning("%d page(s) failed to download:", len(failures))
        for url in failures:
            log.warning("  FAILED PAGE: %s", url)

    if failed_images:
        log.warning("%d image(s) failed to download:", len(failed_images))
        for url in failed_images:
            log.warning("  FAILED IMAGE: %s", url)

    log.info("Recovery complete")
    log.info("Posts processed: %d", len(posts))
    log.info(
        "Featured images: %d found, %d downloaded",
        total_featured_found, total_featured_downloaded,
    )
    log.info(
        "Content images: %d found, %d downloaded",
        total_content_found, total_content_downloaded,
    )
    log.info("Failed pages: %d, Failed images: %d", len(failures), len(failed_images))
    return 0 if posts else 1


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wayback_recover.py",
        description="Recover WordPress blog posts and images from the Wayback Machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--index-url",
        required=True,
        metavar="URL",
        help=(
            "Original site URL to recover (e.g. https://example.com/), "
            "or a direct Wayback Machine archive URL "
            "(e.g. https://web.archive.org/web/TIMESTAMP/https://example.com/blog/)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        metavar="DIR",
        help="Directory to write recovered content.",
    )
    parser.add_argument(
        "--mode",
        choices=["dry-run", "full"],
        default="dry-run",
        help="dry-run: list snapshots only.  full: download and export WXR. (default: dry-run)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)

    log.info("wayback-recover-bot starting — mode=%s, index-url=%s", args.mode, args.index_url)

    if args.mode == "dry-run":
        return run_dry(args.index_url)
    else:
        return run_full(args.index_url, output_dir)


if __name__ == "__main__":
    sys.exit(main())
