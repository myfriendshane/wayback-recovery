#!/usr/bin/env python3
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
)

def is_blacklisted_url(url: str) -> bool:
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
    if parsed.query:
        q = parsed.query.lower()
        if any(x in q for x in ("q=", "merchant", "product", "c=")):
            return True
    return False

def is_likely_published_post(html: str, url: str) -> bool:
    if not html:
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
