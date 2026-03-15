# wayback-recovery

Recover WordPress posts and assets from the [Wayback Machine](https://web.archive.org/)
and export them as a WXR file ready to import into a new WordPress installation.

---

## Table of Contents

1. [Overview](#overview)
2. [Installation](#installation)
3. [Quick Start](#quick-start)
4. [Usage](#usage)
5. [Asset Storage Alternatives](#asset-storage-alternatives)
6. [Legal / Copyright Note](#legal--copyright-note)

---

## Overview

`scripts/wayback_recover.py` is a Python 3 CLI tool that:

- Queries the **CDX API** to discover all archived snapshots of your site.
- Downloads archived HTML pages from `web.archive.org`.
- Extracts images, stylesheets, and scripts referenced in each page.
- In **full** mode: downloads all assets locally and rewrites their URLs to
  relative paths, then writes a `wxr_output.xml` WXR file you can import into
  WordPress (`Tools → Import → WordPress`).
- Handles rate-limiting (HTTP 429 / 503) with exponential back-off and respects
  the `Retry-After` response header.

---

## Installation

**Requirements:** Python 3.10+ (Python 3.11 recommended, matching the Dockerfile)

```bash
# Clone the repository
git clone https://github.com/myfriendshane/wayback-recovery.git
cd wayback-recovery

# Install dependencies
pip install -r requirements.txt
```

Or run inside Docker (see [Dockerfile](Dockerfile)):

```bash
docker build -t wayback-recovery .
docker run --rm wayback-recovery          # prints help
```

---

## Quick Start

### Dry-run — list available snapshots (no files written)

```bash
python scripts/wayback_recover.py \
    --index-url "https://example.com/" \
    --output-dir ./output \
    --mode dry-run
```

### Full recovery — download HTML, assets, and write WXR

```bash
python scripts/wayback_recover.py \
    --index-url "https://example.com/" \
    --output-dir ./output \
    --mode full
```

After a successful full run you will find:

```
output/
├── html/          # raw archived HTML files
├── assets/        # downloaded images, CSS, JS (preserves URL path structure)
└── wxr_output.xml # WordPress import file
```

Import `wxr_output.xml` via **WordPress Admin → Tools → Import → WordPress**.

---

## Usage

```
usage: wayback_recover.py [-h] --index-url URL --output-dir DIR [--mode {dry-run,full}]

options:
  -h, --help           show this help message and exit
  --index-url URL      Original site URL to recover (e.g. https://example.com/).
  --output-dir DIR     Directory to write recovered content.
  --mode {dry-run,full}
                       dry-run: list snapshots only.
                       full:    download and export WXR. (default: dry-run)
```

**Exit codes:** `0` on success, `1` on fatal failure.

---

## Asset Storage Alternatives

After recovery, your site's assets (images, etc.) live in `output/assets/`.
Below are your options for hosting them long-term.

### 1. Local Filesystem (default)

Assets are already saved under `output/assets/` with their original URL path
structure intact.  Serve them from any web server by copying the directory.

```bash
# Example: copy assets into a WordPress uploads folder
cp -r output/assets/ /var/www/html/wp-content/uploads/
```

**Tradeoffs:** Simplest option; requires sufficient disk space on the server.

---

### 2. MinIO (self-hosted S3-compatible)

[MinIO](https://min.io/) is a self-hosted object store with an S3-compatible API.

```bash
# Start MinIO locally
docker run -p 9000:9000 -p 9001:9001 \
    -e "MINIO_ROOT_USER=admin" \
    -e "MINIO_ROOT_PASSWORD=password" \
    quay.io/minio/minio server /data --console-address ":9001"

# Upload recovered assets (requires mc — MinIO Client)
mc alias set local http://localhost:9000 admin password
mc mb local/wayback-assets
mc cp --recursive output/assets/ local/wayback-assets/
```

**Tradeoffs:** Full control; works offline; requires running a MinIO server.

---

### 3. Git LFS

Store large binary files in Git using [Git LFS](https://git-lfs.github.com/).

```bash
git lfs install
git lfs track "output/assets/**"
git add .gitattributes output/assets/
git commit -m "Add recovered assets via Git LFS"
git push
```

**Tradeoffs:** Keeps assets in source control; GitHub free tier has 1 GB LFS
storage; suitable for small sites.

---

### 4. GitHub Releases / GitHub Pages

Upload an assets archive as a [GitHub Release](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)
asset, or publish a static site via [GitHub Pages](https://pages.github.com/).

```bash
# Create a zip of assets and attach to a release via GitHub CLI
zip -r assets.zip output/assets/
gh release create v1.0.0 assets.zip --title "Recovered assets"
```

**Tradeoffs:** Free for public repos; release assets have a 2 GB per-file limit;
Pages works well for static HTML + assets.

---

### 5. CDN via Netlify or Cloudflare Pages

Push the `output/` directory to a Git repository and connect it to
[Netlify](https://www.netlify.com/) or [Cloudflare Pages](https://pages.cloudflare.com/)
for automatic CDN delivery.

```bash
# Netlify CLI
npm install -g netlify-cli
netlify deploy --dir=output/ --prod
```

**Tradeoffs:** Global CDN with generous free tiers; easiest public hosting
option; requires the output directory to be in a Git repo.

---

### 6. Self-Hosted Server via rsync

Copy assets to any Linux server using `rsync` over SSH.

```bash
rsync -avz --progress output/assets/ user@yourserver.com:/var/www/html/assets/
```

**Tradeoffs:** Full control; requires SSH access and a running web server
(e.g. Nginx/Apache); no vendor lock-in.

---

## Legal / Copyright Note

> **Important:** You should only use this tool to recover content that you own
> or have explicit rights to.  The user of this tool confirms they own (or have
> permission to republish) the content being recovered.  Recovering and
> republishing third-party content without permission may violate copyright law.
> The Internet Archive's [Terms of Service](https://archive.org/about/terms.php)
> also apply to all Wayback Machine access.

