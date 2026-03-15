#!/usr/bin/env python3
"""
wayback_recover.py

Updated: unwrap Wayback URLs and try multiple CDX query variants for robustness.

Usage and behavior unchanged. See README.md for details.
"""
from __future__ import annotations
import argparse
import requests
import logging
import time
import os
import re
import hashlib
import sys
from urllib.parse import urlparse, urljoin, unquote
from bs4 import BeautifulSoup
import post_filters
from xml.sax.saxutils import escape as xml_escape

# Config
CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_ARCHIVE_TEMPLATE = "https://web.archive.org/web/{timestamp}/{url}"
USER_AGENT = "wayback-recover-bot/1.0 (https://github.com/myfriendshane/wayback-recovery)"
REQUEST_TIMEOUT = 45  # seconds
MAX_CDX_RETRIES = 3
CDX_RETRY_BACKOFF = 1.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# network fetch helper with retries (used for general HTTP fetches)
def fetch_with_retry(url: str, max_retries: int = 3, backoff: float = 1.0, stream: bool = False) -> requests.Response | None:
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT, stream=stream)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            last_exc = e
            status = getattr(e.response, 'status_code', None)
            logging.warning("HTTP error %s for %s", status, url)
            # Respect Retry-After on rate limits
            if e.response is not None and e.response.status_code in (429, 503):
                ra = e.response.headers.get("Retry-After")
                try:
                    wait = int(ra) if ra and ra.isdigit() else backoff * attempt
                except Exception:
                    wait = backoff * attempt
                logging.info("Retry-After: sleeping %s seconds", wait)
                time.sleep(wait)
            else:
                time.sleep(backoff * attempt)
        except requests.RequestException as e:
            last_exc = e
            logging.warning("Network error on attempt %d for %s: %s", attempt, url, e)
            time.sleep(backoff * attempt)
    logging.error("Failed to fetch %s after %d attempts: %s", url, max_retries, last_exc)
    return None

# Helper to unwrap wayback-wrapped URLs like /web/<ts>/https://example.com/path or full wayback URLs
def unwrap_wayback_url(url: str) -> str:
    if not url:
        return url
    # If contains web.archive.org/web/<ts>/http(s)://... extract the original
    m = re.search(r"/web/\d{1,14}/(https?://.+)$", url)
    if m:
        try:
            return unquote(m.group(1))
        except Exception:
            return m.group(1)
    # if starts with /web/<ts>/https://... (relative link on wayback host)
    m2 = re.search(r"^/web/\d{1,14}/(https?://.+)$", url)
    if m2:
        return unquote(m2.group(1))
    return url

# Build CDX query URL helper
import urllib.parse

def cdx_query_url(params: dict) -> str:
    return CDX_API + "?" + urllib.parse.urlencode(params, safe=':*')

# Try multiple CDX query variants until results are found
def query_cdx(original_url: str, limit: int = 50) -> list:
    """
    Query CDX for snapshots of original_url. This will try several URL variants (exact, scheme swap, www/non-www, prefix wildcard) until it finds results.
    Returns CDX rows or empty list.
    """
    # Unwrap if the user passed a wayback-wrapped URL
    u = unwrap_wayback_url(original_url)
    logging.info("Querying CDX for: %s", u)

    variants = []
    # exact
    variants.append((u, {'url': u, 'output': 'json', 'filter': 'statuscode:200', 'limit': str(limit), 'collapse': 'digest'}))

    # scheme swap
    parsed = urlparse(u)
    if parsed.scheme in ('http','https'):
        alt_scheme = 'https' if parsed.scheme == 'http' else 'http'
        swapped = parsed._replace(scheme=alt_scheme).geturl()
        if swapped != u:
            variants.append((swapped, {'url': swapped, 'output': 'json', 'filter': 'statuscode:200', 'limit': str(limit), 'collapse': 'digest'}))

    # www/non-www variants
    host = parsed.netloc
    if host.startswith('www.'):
        nonwww = host[4:]
        v = parsed._replace(netloc=nonwww).geturl()
        variants.append((v, {'url': v, 'output': 'json', 'filter': 'statuscode:200', 'limit': str(limit), 'collapse': 'digest'}))
    else:
        www = 'www.' + host
        v = parsed._replace(netloc=www).geturl()
        variants.append((v, {'url': v, 'output': 'json', 'filter': 'statuscode:200', 'limit': str(limit), 'collapse': 'digest'}))

    # prefix wildcard (try with matchType=prefix and wildcard)
    try:
        wildcard = u.rstrip('/') + '/*'
        variants.append((wildcard, {'url': wildcard, 'matchType': 'prefix', 'output': 'json', 'filter': 'statuscode:200', 'limit': str(limit), 'collapse': 'urlkey', 'sort': 'reverse', 'fl': 'timestamp,original,mimetype,statuscode,digest,length'}))
    except Exception:
        pass

    # dedupe variants while preserving order
    seen = set()
    deduped = []
    for v,p in variants:
        key = p.get('url')
        if key and key not in seen:
            deduped.append((v,p))
            seen.add(key)

    for variant_url, params in deduped:
        q = cdx_query_url(params)
        # try a few times but do not hang too long on a single variant
        rows = None
        for attempt in range(1, MAX_CDX_RETRIES + 1):
            try:
                resp = SESSION.get(q, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                if data and len(data) > 1:
                    logging.info("CDX variant succeeded: %s", variant_url)
                    return data
                else:
                    break
            except requests.RequestException as e:
                logging.warning("Network error on attempt %d for %s: %s — retrying", attempt, q, e)
                time.sleep(CDX_RETRY_BACKOFF * attempt)
        # continue to next variant
    logging.warning("CDX returned no results for %s after trying variants", original_url)
    return []

# The rest of the script is left unchanged; reuse existing helpers for parsing and download

def archived_url(timestamp: str, original: str) -> str:
    return WAYBACK_ARCHIVE_TEMPLATE.format(timestamp=timestamp, url=original)

def extract_links_from_index_html(archive_html: str) -> list:
    soup = BeautifulSoup(archive_html, "html.parser")
    anchors = soup.find_all("a", href=True)
    urls = set()
    for a in anchors:
        href = a["href"].strip()
        if not href:
            continue
        # unwrap wayback links
        m = re.search(r"/web/\d{1,14}/(https?://.+)$", href)
        if m:
            orig = unquote(m.group(1))
            urls.add(orig)
            continue
        m2 = re.search(r"https?://web\.archive\.org/web/\d{1,14}/(https?://.+)$", href)
        if m2:
            orig = unquote(m2.group(1))
            urls.add(orig)
            continue
        if href.startswith("http"):
            if "currylovers.co.za" in href:
                urls.add(href.split("#")[0].rstrip("/"))
            continue
        if href.startswith("/"):
            urls.add("https://www.currylovers.co.za" + href.split("#")[0].rstrip("/"))
    return sorted(urls)

def extract_assets_from_html(html: str, base_url: str) -> set:
    soup = BeautifulSoup(html, "html.parser")
    assets = set()
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        src = src.strip()
        if src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("/"):
            parsed = urlparse(base_url)
            src = f"{parsed.scheme}://{parsed.netloc}{src}"
        if src.startswith("http"):
            assets.add(src)
    for script in soup.find_all("script"):
        src = script.get("src")
        if not src:
            continue
        src = src.strip()
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("/"):
            parsed = urlparse(base_url)
            src = f"{parsed.scheme}://{parsed.netloc}{src}"
        if src.startswith("http"):
            assets.add(src)
    for link in soup.find_all("link"):
        href = link.get("href")
        if not href:
            continue
        href = href.strip()
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/"):
            parsed = urlparse(base_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        if href.startswith("http"):
            assets.add(href)
    return assets

def download_asset(archived_asset_url: str, dest_dir: str) -> str | None:
    r = fetch_with_retry(archived_asset_url, max_retries=2, backoff=1.0)
    if r is None:
        return None
    parsed = urlparse(archived_asset_url)
    fname = os.path.basename(parsed.path) or "asset"
    h = hashlib.sha1(archived_asset_url.encode("utf-8")).hexdigest()[:8]
    fname = f"{h}_{fname}"
    os.makedirs(dest_dir, exist_ok=True)
    out_path = os.path.join(dest_dir, fname)
    try:
        with open(out_path, "wb") as fh:
            fh.write(r.content)
        return out_path
    except Exception as e:
        logging.error("Failed to write asset %s to %s: %s", archived_asset_url, out_path, e)
        return None

def safe_filename_from_url(u: str) -> str:
    parsed = urlparse(u)
    base = parsed.path.rstrip("/").split("/")[-1] or "post"
    base = re.sub(r"[^a-zA-Z0-9._-]", "_", base)
    return base

def make_minimal_wxr_item(title: str, link: str, pubdate: str, content_html: str, guid: str) -> str:
    item = []
    item.append("<item>")
    item.append(f"<title>{xml_escape(title)}</title>")
    item.append(f"<link>{xml_escape(link)}</link>")
    item.append(f"<pubDate>{xml_escape(pubdate)}</pubDate>")
    item.append("<dc:creator><![CDATA[recovered]]></dc:creator>")
    item.append("<content:encoded><![CDATA[")
    item.append(content_html)
    item.append("]]></content:encoded>")
    item.append(f"<guid isPermaLink=\"false\">{xml_escape(guid)}</guid>")
    item.append("<wp:post_type>post</wp:post_type>")
    item.append("</item>")
    return "\n".join(item)

def process_article(orig: str, mode: str, output_dir: str, target_ts: str | None = None):
    # skip known non-post URLs early
    if post_filters.is_blacklisted_url(orig):
        logging.info('Skipping blacklisted URL: %s', orig)
        return None
    rows = query_cdx(orig, limit=100)
    if not rows:
        logging.warning("No CDX snapshots found for %s; skipping", orig)
        return None
    # choose timestamp logic: prefer latest <= target_ts if provided
    timestamps = [row[1] for row in rows[1:] if len(row) > 1]
    chosen_ts = None
    if target_ts:
        valid = [t for t in timestamps if t <= target_ts]
        chosen_ts = max(valid) if valid else max(timestamps)
    else:
        chosen_ts = max(timestamps)
    aurl = archived_url(chosen_ts, orig)
    logging.info("Fetching archived version %s", aurl)
    resp = fetch_with_retry(aurl, max_retries=3, backoff=1.0)
    if resp is None:
        logging.warning("Failed to download archived HTML for %s; skipping", orig)
        return None
    html = resp.text
    # Verify this looks like a published post (skip pages like checkout/product/contact)
    try:
        if not post_filters.is_likely_published_post(html, orig):
            logging.info('Skipping non-post page (not a published post): %s', orig)
            return None
    except Exception as _e:
        logging.warning('Post detection failed for %s: %s', orig, _e)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else safe_filename_from_url(orig)
    pubdate = ""
    for meta_name in ("article:published_time", "pubdate", "date", "DC.date.issued"):
        meta = soup.find("meta", attrs={"property": meta_name}) or soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            pubdate = meta["content"]
            break
    if not pubdate:
        pubdate = chosen_ts
    assets = extract_assets_from_html(html, orig)
    archived_asset_urls = [archived_url(chosen_ts, a) for a in assets]
    logging.info("Found %d assets for this post", len(archived_asset_urls))
    local_asset_paths = []
    if mode == 'full':
        assets_dir = os.path.join(output_dir, 'assets')
        for av in archived_asset_urls:
            lp = download_asset(av, assets_dir)
            if lp:
                local_asset_paths.append((av, lp))
        for av, lp in local_asset_paths:
            rel = os.path.relpath(lp, output_dir).replace('\\', '/')
            html = html.replace(av, rel)
    guid = f"recovered-{hashlib.sha1((orig + chosen_ts).encode()).hexdigest()}"
    wxr_item = make_minimal_wxr_item(title=title, link=orig, pubdate=pubdate, content_html=html, guid=guid)
    return wxr_item

def write_wxr(items: list, output_dir: str):
    wxr_path = os.path.join(output_dir, 'wxr_output.xml')
    try:
        with open(wxr_path, 'w', encoding='utf-8') as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8" ?>\n')
            fh.write('<rss version="2.0"\n')
            fh.write('    xmlns:content="http://purl.org/rss/1.0/modules/content/"\n')
            fh.write('    xmlns:wfw="http://wellformedweb.org/CommentAPI/"\n')
            fh.write('    xmlns:dc="http://purl.org/dc/elements/1.1/"\n')
            fh.write('    xmlns:wp="http://wordpress.org/export/1.2/"\n')
            fh.write('>\n')
            fh.write('<channel>\n')
            fh.write('<title>Recovered site</title>\n')
            for it in items:
                fh.write(it + "\n")
            fh.write('</channel>\n')
            fh.write('</rss>\n')
        logging.info("WXR written successfully to %s", wxr_path)
    except Exception as e:
        logging.error("Failed to write WXR: %s", e)

def run_dry(index_url: str):
    # If given a wayback index URL, unwrap it
    orig_index = unwrap_wayback_url(index_url)
    # If index is still a wayback URL (e.g., was full wayback URL), try to fetch the archived HTML directly
    try:
        logging.info("Fetching index page %s", index_url)
        resp = fetch_with_retry(index_url, max_retries=2, backoff=1.0)
        if resp is None:
            logging.error("Cannot fetch index page; aborting.")
            return 1
        index_html = resp.text
    except Exception as e:
        logging.error("Failed to fetch index page: %s", e)
        return 1
    article_urls = extract_links_from_index_html(index_html)
    logging.info("Discovered %d candidate article URLs", len(article_urls))
    if not article_urls:
        logging.error("No article URLs found under index page; aborting.")
        return 1
    for orig in article_urls:
        logging.info("Processing article %s", orig)
        item = process_article(orig, mode='dry-run', output_dir='output')
        if item:
            print('Found post:', orig)
    logging.info('Dry-run complete.')
    return 0

def run_full(index_url: str, output_dir: str):
    resp = fetch_with_retry(index_url, max_retries=2, backoff=1.0)
    if resp is None:
        logging.error('Cannot fetch index page; aborting.')
        return 1
    index_html = resp.text
    article_urls = extract_links_from_index_html(index_html)
    items = []
    for orig in article_urls:
        logging.info('Processing article %s', orig)
        wxr_item = process_article(orig, mode='full', output_dir=output_dir, target_ts=None)
        if wxr_item:
            items.append(wxr_item)
        time.sleep(0.5)
    write_wxr(items, output_dir)
    return 0

def main():
    parser = argparse.ArgumentParser(description='Recover WordPress posts from a Wayback index page.')
    parser.add_argument('--index-url', required=True, help='Wayback index URL (the Curry School page you provided).')
    parser.add_argument('--output-dir', default='output', help='Directory to write output files and assets.')
    parser.add_argument('--mode', choices=('dry-run','full'), default='dry-run', help='dry-run: list; full: download and produce WXR.')
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    logging.info('wayback-recover-bot starting — mode=%s, index-url=%s', args.mode, args.index_url)
    if args.mode == 'dry-run':
        return run_dry(args.index_url)
    return run_full(args.index_url, args.output_dir)

if __name__ == '__main__':
    sys.exit(main())
