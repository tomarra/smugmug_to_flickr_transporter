#!/usr/bin/env python3
"""
SmugMug → Flickr Migration Script
===================================
Migrates all photos and albums from SmugMug to Flickr, preserving:
  - Album/photoset structure
  - Photo titles and descriptions
  - Tags
  - Original image quality

Features:
  - Resumable: saves progress to migration_progress.json so it can
    be interrupted and restarted without re-uploading already-done photos
  - Streams photos (download → upload → delete temp file) to avoid
    filling up your disk
  - Rate-limit aware with automatic retries
  - Detailed logging to migration.log

Requirements: See requirements.txt
Usage: See README.md
"""

import os
import sys
import json
import time
import logging
import tempfile
import argparse
from pathlib import Path
from datetime import datetime

import requests
from requests_oauthlib import OAuth1Session
import flickrapi

# ─────────────────────────────────────────────
# CONFIGURATION — fill these in before running
# ─────────────────────────────────────────────

SMUGMUG_API_KEY        = os.environ.get("SMUGMUG_API_KEY", "YOUR_SMUGMUG_API_KEY")
SMUGMUG_API_SECRET     = os.environ.get("SMUGMUG_API_SECRET", "YOUR_SMUGMUG_API_SECRET")
SMUGMUG_ACCESS_TOKEN   = os.environ.get("SMUGMUG_ACCESS_TOKEN", "")
SMUGMUG_ACCESS_SECRET  = os.environ.get("SMUGMUG_ACCESS_SECRET", "")
SMUGMUG_NICKNAME       = os.environ.get("SMUGMUG_NICKNAME", "")  # Your SmugMug username/nickname

FLICKR_API_KEY         = os.environ.get("FLICKR_API_KEY", "YOUR_FLICKR_API_KEY")
FLICKR_API_SECRET      = os.environ.get("FLICKR_API_SECRET", "YOUR_FLICKR_API_SECRET")

PROGRESS_FILE = "migration_progress.json"
LOG_FILE      = "migration.log"

# Delay between API calls (seconds) — increase if you hit rate limits
REQUEST_DELAY = 0.5

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# PROGRESS TRACKING
# ─────────────────────────────────────────────

def load_progress():
    """Load migration progress from disk (for resuming interrupted runs)."""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {
        "uploaded_images":  {},   # smugmug_image_uri → flickr_photo_id
        "created_albums":   {},   # smugmug_album_uri → flickr_photoset_id
        "album_first_photos": {}, # smugmug_album_uri → first flickr_photo_id (for resume)
        "completed_albums": [],   # list of fully-migrated smugmug album URIs
    }

def save_progress(progress):
    """Persist migration progress to disk."""
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ─────────────────────────────────────────────
# SMUGMUG CLIENT
# ─────────────────────────────────────────────

class SmugMugClient:
    BASE_URL = "https://api.smugmug.com"

    def __init__(self, api_key, api_secret, access_token, access_secret):
        self.session = OAuth1Session(
            client_key=api_key,
            client_secret=api_secret,
            resource_owner_key=access_token,
            resource_owner_secret=access_secret,
        )

    def get(self, path, params=None):
        """Make a GET request to the SmugMug API v2."""
        url = f"{self.BASE_URL}{path}"
        default_params = {"_verbosity": "1"}
        if params:
            default_params.update(params)
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=default_params,
                                        headers={"Accept": "application/json"})
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    wait = 60 * (attempt + 1)
                    log.warning(f"Rate limited by SmugMug. Waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"SmugMug API failed after 3 attempts: {path}")

    def get_all_pages(self, path, params=None):
        """Yield all items across paginated SmugMug responses."""
        params = params or {}
        params["count"] = 100
        params["start"] = 1
        while True:
            data = self.get(path, params)
            response = data.get("Response", {})
            # Find the first list in the response (album list, image list, etc.)
            items = None
            for key, val in response.items():
                if isinstance(val, list):
                    items = val
                    break
            if not items:
                break
            yield from items
            pages = response.get("Pages", {})
            next_page = pages.get("NextPage")
            if not next_page:
                break
            # Advance using actual returned Start + Count to avoid off-by-one
            returned_start = pages.get("Start", params["start"])
            returned_count = pages.get("Count", len(items))
            params["start"] = returned_start + returned_count
            time.sleep(REQUEST_DELAY)

    def get_albums(self, nickname):
        """List all albums for a user."""
        path = f"/api/v2/user/{nickname}!albums"
        return list(self.get_all_pages(path))

    def get_album_images(self, album_uri):
        """List all images in an album."""
        path = f"{album_uri}!images"
        return list(self.get_all_pages(path))

    def get_image_download_url(self, image_data):
        """
        Get the best available download URL for an image.

        Strategy (in order):
          1. ArchivedUri  — original file URL, already present in the image listing data
          2. !imageSizes  — SmugMug sizes endpoint, returns OriginalUrl and fallbacks
          3. ThumbnailUrl — last resort (lower quality)
        """
        # Option 1: ArchivedUri is the direct original-file URL and is usually
        # already included in the image listing response.
        archived = image_data.get("ArchivedUri")
        if archived:
            return archived

        # Option 2: Fetch sizes via the image-specific endpoint.
        # The image URI from an album listing looks like:
        #   /api/v2/album/ALBUMKEY/image/IMAGEKEY-VERSION
        # The sizes endpoint lives at:
        #   /api/v2/image/IMAGEKEY-VERSION!sizes
        img_uri = image_data.get("Uri", "")
        try:
            # Extract the image key (everything after the last "/image/")
            image_key = img_uri.rsplit("/image/", 1)[-1]
            data = self.get(f"/api/v2/image/{image_key}!sizes")
            sizes = data.get("Response", {}).get("ImageSizes", {})
            for size_key in ["OriginalUrl", "X5Url", "X4Url", "X3Url", "X2Url",
                              "XLargeUrl", "LargeUrl", "MediumUrl"]:
                url = sizes.get(size_key)
                if url:
                    return url
        except Exception as e:
            log.debug(f"!sizes fallback failed for {img_uri}: {e}")

        # Option 3: Use thumbnail as last resort (low quality, but better than nothing)
        return image_data.get("ThumbnailUrl")

    def download_image(self, url, dest_path):
        """Download an image file to dest_path."""
        for attempt in range(3):
            try:
                with self.session.get(url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(dest_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            f.write(chunk)
                return
            except Exception as e:
                if attempt == 2:
                    raise
                log.warning(f"Download attempt {attempt+1} failed: {e}. Retrying...")
                time.sleep(5)


# ─────────────────────────────────────────────
# FLICKR CLIENT
# ─────────────────────────────────────────────

class FlickrClient:
    def __init__(self, api_key, api_secret):
        self.flickr = flickrapi.FlickrAPI(api_key, api_secret, format="parsed-json")
        self._authenticate()

    def _authenticate(self):
        """Run OAuth flow if not already authenticated."""
        if not self.flickr.token_valid(perms="write"):
            log.info("Flickr authentication required. Opening browser...")
            self.flickr.get_request_token(oauth_callback="oob")
            authorize_url = self.flickr.auth_url(perms="write")
            print(f"\nVisit this URL to authorize the app with Flickr:\n{authorize_url}\n")
            verifier = input("Enter the code from Flickr: ").strip()
            self.flickr.get_access_token(verifier)
        log.info("Flickr authenticated successfully.")

    def upload_photo(self, filepath, title, description, tags):
        """Upload a photo and return its Flickr photo ID."""
        tag_str = " ".join(f'"{t}"' if " " in t else t for t in tags) if tags else ""
        for attempt in range(3):
            try:
                resp = self.flickr.upload(
                    filename=filepath,
                    title=title,
                    description=description,
                    tags=tag_str,
                    is_public=1,   # public visibility
                    format="etree",
                )
                photoid_elem = resp.find("photoid")
                if photoid_elem is None:
                    raise RuntimeError("Flickr upload response missing photoid element")
                photo_id = photoid_elem.text
                time.sleep(REQUEST_DELAY)
                return photo_id
            except Exception as e:
                if attempt == 2:
                    raise
                log.warning(f"Upload attempt {attempt+1} failed: {e}. Retrying in 10s...")
                time.sleep(10)

    def create_photoset(self, title, description, primary_photo_id):
        """Create a Flickr photoset (album) and return its ID."""
        resp = self.flickr.photosets.create(
            title=title,
            description=description or "",
            primary_photo_id=primary_photo_id,
        )
        return resp["photoset"]["id"]

    def add_photo_to_photoset(self, photoset_id, photo_id):
        """Add a photo to an existing photoset."""
        self.flickr.photosets.addPhoto(photoset_id=photoset_id, photo_id=photo_id)
        time.sleep(REQUEST_DELAY)


# ─────────────────────────────────────────────
# SMUGMUG OAUTH HELPER (first-time setup)
# ─────────────────────────────────────────────

def authorize_smugmug(api_key, api_secret):
    """Interactive OAuth flow to get SmugMug access tokens."""
    REQUEST_TOKEN_URL  = "https://api.smugmug.com/services/oauth/1.0a/getRequestToken"
    AUTHORIZE_URL      = "https://api.smugmug.com/services/oauth/1.0a/authorize"
    ACCESS_TOKEN_URL   = "https://api.smugmug.com/services/oauth/1.0a/getAccessToken"

    oauth = OAuth1Session(api_key, client_secret=api_secret, callback_uri="oob")
    oauth.fetch_request_token(REQUEST_TOKEN_URL)
    auth_url = oauth.authorization_url(AUTHORIZE_URL, access="Full", permissions="Read")

    print(f"\nVisit this URL to authorize the app with SmugMug:\n{auth_url}\n")
    verifier = input("Enter the 6-digit PIN from SmugMug: ").strip()

    oauth = OAuth1Session(api_key, client_secret=api_secret,
                          resource_owner_key=oauth.token["oauth_token"],
                          resource_owner_secret=oauth.token["oauth_token_secret"])
    tokens = oauth.fetch_access_token(ACCESS_TOKEN_URL, verifier=verifier)
    print("\nSave these in your environment (or .env file):")
    print(f"  SMUGMUG_ACCESS_TOKEN={tokens['oauth_token']}")
    print(f"  SMUGMUG_ACCESS_SECRET={tokens['oauth_token_secret']}")
    return tokens["oauth_token"], tokens["oauth_token_secret"]


# ─────────────────────────────────────────────
# MIGRATION ORCHESTRATOR
# ─────────────────────────────────────────────

def migrate(smugmug: SmugMugClient, flickr: FlickrClient, nickname: str):
    progress = load_progress()
    # Ensure album_first_photos key exists for older progress files
    if "album_first_photos" not in progress:
        progress["album_first_photos"] = {}

    stats = {"albums": 0, "photos": 0, "skipped": 0, "errors": 0}

    log.info(f"Fetching albums for SmugMug user: {nickname}")
    albums = smugmug.get_albums(nickname)
    log.info(f"Found {len(albums)} albums.")

    for album in albums:
        album_uri   = album.get("Uri", "")
        album_title = album.get("Name", "Untitled Album")
        album_desc  = album.get("Description", "")

        if album_uri in progress["completed_albums"]:
            log.info(f"[SKIP] Album already migrated: {album_title}")
            stats["skipped"] += 1
            continue

        log.info(f"\n{'='*60}")
        log.info(f"Processing album: {album_title} ({album_uri})")

        images = smugmug.get_album_images(album_uri)
        log.info(f"  Found {len(images)} images in album.")

        if not images:
            log.info(f"  Album is empty, skipping.")
            progress["completed_albums"].append(album_uri)
            save_progress(progress)
            continue

        flickr_photoset_id = progress["created_albums"].get(album_uri)
        # Restore first-photo tracking so resume doesn't re-add it to the photoset
        first_photo_id_for_album = progress["album_first_photos"].get(album_uri)

        with tempfile.TemporaryDirectory() as tmpdir:
            for img in images:
                img_uri   = img.get("Uri", "")
                img_title = img.get("Title") or img.get("FileName", "Untitled")
                img_desc  = img.get("Caption", "")
                img_tags  = [kw.strip() for kw in (img.get("Keywords") or "").split(",") if kw.strip()]

                # Already uploaded?
                if img_uri in progress["uploaded_images"]:
                    existing_id = progress["uploaded_images"][img_uri]
                    # Make sure it's in the photoset (handles partial resume)
                    if flickr_photoset_id and existing_id != first_photo_id_for_album:
                        try:
                            flickr.add_photo_to_photoset(flickr_photoset_id, existing_id)
                        except Exception:
                            pass  # may already be in the set
                    log.info(f"  [SKIP] Already uploaded: {img_title}")
                    stats["skipped"] += 1
                    continue

                # Get download URL from image data (no extra API call needed in most cases)
                dl_url = smugmug.get_image_download_url(img)
                if not dl_url:
                    log.warning(f"  [WARN] No download URL found for: {img_title} — skipping")
                    stats["errors"] += 1
                    continue

                # Download to temp file
                ext = Path(img.get("FileName", "photo.jpg")).suffix or ".jpg"
                tmp_path = os.path.join(tmpdir, f"photo{ext}")
                try:
                    log.info(f"  Downloading: {img_title}")
                    smugmug.download_image(dl_url, tmp_path)
                except Exception as e:
                    log.error(f"  [ERROR] Download failed for {img_title}: {e}")
                    stats["errors"] += 1
                    continue

                # Upload to Flickr
                try:
                    log.info(f"  Uploading to Flickr: {img_title}")
                    flickr_photo_id = flickr.upload_photo(tmp_path, img_title, img_desc, img_tags)
                    progress["uploaded_images"][img_uri] = flickr_photo_id
                    stats["photos"] += 1
                    log.info(f"  ✓ Uploaded (Flickr ID: {flickr_photo_id})")
                except Exception as e:
                    log.error(f"  [ERROR] Upload failed for {img_title}: {e}")
                    stats["errors"] += 1
                    continue
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

                # Create photoset on first successful photo, or add to existing one
                if flickr_photoset_id is None:
                    try:
                        flickr_photoset_id = flickr.create_photoset(album_title, album_desc, flickr_photo_id)
                        progress["created_albums"][album_uri] = flickr_photoset_id
                        first_photo_id_for_album = flickr_photo_id
                        progress["album_first_photos"][album_uri] = flickr_photo_id
                        log.info(f"  Created Flickr photoset: {album_title} (ID: {flickr_photoset_id})")
                    except Exception as e:
                        log.error(f"  [ERROR] Could not create photoset: {e}")
                else:
                    if flickr_photo_id != first_photo_id_for_album:
                        try:
                            flickr.add_photo_to_photoset(flickr_photoset_id, flickr_photo_id)
                        except Exception as e:
                            log.warning(f"  [WARN] Could not add photo to set: {e}")

                save_progress(progress)
                time.sleep(REQUEST_DELAY)

        progress["completed_albums"].append(album_uri)
        save_progress(progress)
        stats["albums"] += 1
        log.info(f"✓ Album complete: {album_title}")

    log.info(f"\n{'='*60}")
    log.info("MIGRATION COMPLETE")
    log.info(f"  Albums migrated : {stats['albums']}")
    log.info(f"  Photos uploaded : {stats['photos']}")
    log.info(f"  Skipped (done)  : {stats['skipped']}")
    log.info(f"  Errors          : {stats['errors']}")
    if stats["errors"] > 0:
        log.info(f"  Check {LOG_FILE} for details on any errors.")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migrate SmugMug photos to Flickr")
    parser.add_argument("--auth-smugmug", action="store_true",
                        help="Run SmugMug OAuth authorization flow and print tokens")
    args = parser.parse_args()

    if args.auth_smugmug:
        authorize_smugmug(SMUGMUG_API_KEY, SMUGMUG_API_SECRET)
        return

    # Validate config
    missing = []
    if SMUGMUG_API_KEY == "YOUR_SMUGMUG_API_KEY":    missing.append("SMUGMUG_API_KEY")
    if SMUGMUG_API_SECRET == "YOUR_SMUGMUG_API_SECRET": missing.append("SMUGMUG_API_SECRET")
    if not SMUGMUG_ACCESS_TOKEN:  missing.append("SMUGMUG_ACCESS_TOKEN")
    if not SMUGMUG_ACCESS_SECRET: missing.append("SMUGMUG_ACCESS_SECRET")
    if not SMUGMUG_NICKNAME:      missing.append("SMUGMUG_NICKNAME")
    if FLICKR_API_KEY == "YOUR_FLICKR_API_KEY":      missing.append("FLICKR_API_KEY")
    if FLICKR_API_SECRET == "YOUR_FLICKR_API_SECRET": missing.append("FLICKR_API_SECRET")

    if missing:
        print(f"\n⚠️  Missing configuration: {', '.join(missing)}")
        print("Set these as environment variables or edit the config section at the top of migrate.py")
        print("See README.md for full setup instructions.")
        sys.exit(1)

    log.info("Starting SmugMug → Flickr migration")
    log.info(f"Timestamp: {datetime.now().isoformat()}")

    smugmug = SmugMugClient(
        SMUGMUG_API_KEY, SMUGMUG_API_SECRET,
        SMUGMUG_ACCESS_TOKEN, SMUGMUG_ACCESS_SECRET,
    )
    flickr = FlickrClient(FLICKR_API_KEY, FLICKR_API_SECRET)

    migrate(smugmug, flickr, SMUGMUG_NICKNAME)


if __name__ == "__main__":
    main()
