#!/usr/bin/env python3
"""
Post filtering utilities for currylovers.co.za
"""
from __future__ import annotations
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse

BLACKLIST_DOMAINS = {
    "customerreviews.google.com",
}

BLACKLIST_PATH_PREFIXES = (
    "/checkout",
    "/cart",
    "/my-account",
    "/product",
    "/wp-admin",
    "/wp-login",
    "/shop",
    "/resellers",
    "/refund-policy",
    "/shipping-information",
    "/contact",
    "/page/",
    "/category/",
    "/tag/",
    "/author/",
    "/feed/",
    "/wp-content/",
    "/wp-includes/",
    "/wp-json/",
)

BLACKLIST_URL_PATTERNS = (
    "xmlrpc.php",
)

STATIC_PAGE_SUFFIXES = (
    "/about",
    "/privacy",
    "/terms",
    "/disclaimer",
    "/sitemap",
)

def is_blacklisted_url(url: str) -> bool:
    """Check if a URL should be skipped entirely."""
    if not url:
        return True
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    for d in BLACKLIST_DOMAINS:
        if host.endswith(d):
            return True
    for p in BLACKLIST_PATH_PREFIXES:
        if path.lower().startswith(p):
            return True
    for pattern in BLACKLIST_URL_PATTERNS:
        if pattern in url.lower():
            return True
    # Reject URLs with query strings (usually not posts)
    if parsed.query:
        q = parsed.query.lower()
        if any(x in q for x in ("q=", "merchant", "product", "c=")):
            return True
    # Reject static pages by suffix
    path_stripped = path.lower().rstrip("/")
    if any(path_stripped.endswith(s) for s in STATIC_PAGE_SUFFIXES):
        return True
    return False


def is_likely_published_post(html: str, url: str) -> bool:
    """
    Check if HTML looks like a published blog post.
    Must have article indicators and NOT be a page/product.
    """
    if not html:
        return False
    if is_blacklisted_url(url):
        return False
    soup = BeautifulSoup(html, "html.parser")
    og_type = soup.find("meta", attrs={"property": "og:type"})
    if og_type and og_type.get("content", "").lower() == "article":
        return True
    if soup.find("meta", attrs={"property": "article:published_time"}) or soup.find("meta", attrs={"name": "article:published_time"}):
        return True
    if soup.find("article", class_=re.compile(r"(post|hentry)", re.I)):
        return True
    if soup.find(class_=re.compile(r"(entry-content|post-content|post|hentry)", re.I)):
        return True
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(x in path for x in ("/category/", "/tag/", "/author/", "/feed/", "/product/")):
        return False
    title = soup.find("title")
    p_count = len(soup.find_all("p"))
    if title and title.get_text(strip=True) and p_count >= 3:
        return True
    return False
