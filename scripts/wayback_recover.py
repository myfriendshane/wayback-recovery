#!/usr/bin/env python3
"""
wayback_recover.py — Recover WordPress posts and assets from the Wayback Machine.

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

Flags
-----
  --index-url   The original site URL to recover (required).
  --output-dir  Directory to write recovered HTML, assets, and WXR (required).
  --mode        dry-run  — query CDX and list snapshots; no files written.
                full     — download HTML, extract and download assets, write WXR.

Exit codes
----------
  0  Success.
  1  Fatal error (bad arguments, network failure after all retries, etc.).

Requirements
------------
  pip install requests beautifulsoup4 lxml

Notes
-----
  - Respects the Wayback Machine's Retry-After header on 429/503 responses.
  - Uses exponential back-off (up to MAX_RETRIES attempts) on transient errors.
  - Only the latest successful (HTTP 200) CDX snapshot is used per URL.
  - In full mode a minimal WXR file (wxr_output.xml) is written to --output-dir.
  - You must own or have rights to the content you are recovering.
"""

import argparse
import hashlib
import logging
import posixpath
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

def query_cdx(index_url: str) -> list[dict]:
    """
    Query the CDX API for all HTTP-200 HTML snapshots of *index_url* and its sub-pages.

    Returns a list of dicts with keys: timestamp, original, mimetype, statuscode,
    digest, length.  Only the most recent snapshot per URL is kept (sort=reverse
    ensures collapse=urlkey retains the latest capture for each URL).
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
    log.info("CDX returned %d HTML snapshots", len(records))
    return records


# ---------------------------------------------------------------------------
# HTML download and asset extraction
# ---------------------------------------------------------------------------

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


def extract_assets(html: str, base_url: str) -> list[str]:
    """
    Parse *html* and return a deduplicated list of absolute asset URLs
    (images, stylesheets, scripts) that belong to the same host as *base_url*.
    """
    soup = BeautifulSoup(html, "lxml")
    parsed_base = urllib.parse.urlparse(base_url)
    found: list[str] = []
    seen: set[str] = set()

    selectors = [
        ("img",    "src"),
        ("link",   "href"),
        ("script", "src"),
    ]
    for tag, attr in selectors:
        for element in soup.find_all(tag):
            raw = element.get(attr, "")
            if not raw or raw.startswith("data:"):
                continue
            absolute = urllib.parse.urljoin(base_url, raw)
            parsed = urllib.parse.urlparse(absolute)
            # Only keep assets on the same host
            if parsed.netloc != parsed_base.netloc:
                continue
            if absolute not in seen:
                seen.add(absolute)
                found.append(absolute)

    return found


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
    Rewrite asset ``src``/``href`` attribute values in *html* using BeautifulSoup.

    Only ``<img src>``, ``<link href>``, and ``<script src>`` attributes are
    updated, avoiding accidental replacement of text content or unrelated links.
    *asset_map* maps absolute original URLs to local relative paths; both the
    absolute form and the root-relative path form are matched.
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
    for tag, attr in [("img", "src"), ("link", "href"), ("script", "src")]:
        for element in soup.find_all(tag):
            val = element.get(attr, "")
            if val in lookup:
                element[attr] = lookup[val]

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
    """Perform CDX discovery only; print snapshot list; return exit code."""
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
    """Full recovery: download HTML, assets, rewrite URLs, write WXR."""
    output_dir.mkdir(parents=True, exist_ok=True)
    html_dir = output_dir / "html"
    assets_dir = output_dir / "assets"
    html_dir.mkdir(exist_ok=True)
    assets_dir.mkdir(exist_ok=True)

    records = query_cdx(index_url)
    if not records:
        log.error("No snapshots found — nothing to recover.")
        return 1

    posts: list[dict] = []
    failures: list[str] = []

    for i, rec in enumerate(records, 1):
        original = rec["original"]
        timestamp = rec["timestamp"]
        log.info("[%d/%d] Fetching: %s", i, len(records), original)

        html = fetch_html(timestamp, original)
        if html is None:
            failures.append(original)
            continue

        # --- asset extraction and download ---
        assets = extract_assets(html, original)
        asset_map: dict[str, str] = {}
        for asset_url in assets:
            local_path = download_asset(asset_url, assets_dir, timestamp)
            if local_path:
                asset_map[asset_url] = local_path

        # Rewrite asset URLs in the full page HTML (attribute-safe, via BS4)
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
        write_wxr(posts, index_url, output_dir)
    else:
        log.error("No posts were successfully recovered.")

    if failures:
        log.warning("%d URL(s) failed to download:", len(failures))
        for url in failures:
            log.warning("  FAILED: %s", url)

    log.info("Recovery complete. Posts: %d, Failures: %d", len(posts), len(failures))
    return 0 if posts else 1


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wayback_recover.py",
        description="Recover WordPress posts and assets from the Wayback Machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--index-url",
        required=True,
        metavar="URL",
        help="Original site URL to recover (e.g. https://example.com/).",
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
