#!/usr/bin/env python3
"""
Immich → Amazon Photos sync.

Syncs photos from specified Immich albums to Amazon Photos using the
Amazon Drive v1 API directly. Zero external dependencies - stdlib only.

Features:
  - One-way sync: Immich albums → Amazon Photos root folder
  - Deletion: photos removed from Immich albums are trashed in Amazon Photos
  - State tracking: maps Immich asset IDs → Amazon node IDs in a JSON file
  - Deduplication: skips assets already synced (by ID, not content hash)
  - HEIC/JPEG/PNG/etc: uploads whatever Immich serves as the original file
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Constants

AMAZON_DRIVE_URL = "https://www.amazon.com/drive/v1"
AMAZON_CONTENT_URL = "https://content-na.drive.amazonaws.com/cdproxy"
AMAZON_BASE_PARAMS = "asset=ALL&tempLink=false&resourceVersion=V2&ContentType=JSON"

# Runtime config (populated by _configure())

IMMICH_URL: str = ""
IMMICH_API_KEY: str = ""
ALBUMS_TO_SYNC: list[str] = []
AP_HEADERS: dict[str, str] = {}
IMMICH_HEADERS: dict[str, str] = {}
STATE_FILE: Path = Path("/data/state.json")

log = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    """Get a required environment variable or exit with a clear message."""
    value = os.getenv(name)
    if not value:
        print(
            f"Error: required environment variable {name} is not set", file=sys.stderr
        )
        sys.exit(1)
    return value


def _configure() -> None:
    """Read env vars and set up module-level config. Called once from main()."""
    global IMMICH_URL, IMMICH_API_KEY, ALBUMS_TO_SYNC
    global AP_HEADERS, IMMICH_HEADERS, STATE_FILE

    IMMICH_URL = _require_env("IMMICH_URL").rstrip("/")
    IMMICH_API_KEY = _require_env("IMMICH_API_KEY")
    ALBUMS_TO_SYNC = [
        a.strip() for a in _require_env("IMMICH_ALBUMS").split(",") if a.strip()
    ]

    _ubid_key = _require_env("AP_UBID_KEY")
    _at_key = _require_env("AP_AT_KEY")

    session_id = _require_env("AP_SESSION_ID")
    ap_cookie_str = (
        f"session-id={session_id}; "
        f"{_ubid_key}={_require_env('AP_UBID_VALUE')}; "
        f"{_at_key}={_require_env('AP_AT_VALUE')}"
    )
    AP_HEADERS = {
        "Cookie": ap_cookie_str,
        "x-amzn-sessionid": session_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    IMMICH_HEADERS = {"x-api-key": IMMICH_API_KEY}

    state_dir = Path(os.getenv("STATE_DIR", "/data"))
    STATE_FILE = state_dir / "state.json"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(state_dir / "immich-sync.log"),
        ],
    )


# HTTP helpers


def _url_with_params(base: str, extra_params: str = "") -> str:
    params = AMAZON_BASE_PARAMS
    if extra_params:
        params += f"&{extra_params}"
    return f"{base}?{params}"


def _request(
    url: str,
    *,
    headers: dict | None = None,
    data: bytes | None = None,
    method: str | None = None,
    timeout: int = 30,
) -> bytes:
    """Make an HTTP request and return the response body. Raises on non-2xx."""
    req = Request(url, data=data, headers=headers or {}, method=method)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _json_request(
    url: str,
    *,
    headers: dict | None = None,
    data: bytes | None = None,
    method: str | None = None,
    timeout: int = 30,
) -> dict:
    body = _request(url, headers=headers, data=data, method=method, timeout=timeout)
    return json.loads(body)


# Multipart form-data encoder


def _encode_multipart(
    fields: dict[str, tuple[str | None, bytes, str]],
) -> tuple[bytes, str]:
    """Encode fields as multipart/form-data.

    Each field value is a tuple of (filename | None, data_bytes, content_type).
    Returns (body_bytes, content_type_header).
    """
    boundary = f"----FormBoundary{uuid.uuid4().hex[:16]}"
    parts: list[bytes] = []

    for field_name, (filename, data, content_type) in fields.items():
        disposition = f'form-data; name="{field_name}"'
        if filename:
            disposition += f'; filename="{filename}"'

        part = (
            f"--{boundary}\r\n"
            f"Content-Disposition: {disposition}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"\r\n"
        ).encode()
        parts.append(part + data + b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


# State
# state.json schema: {"synced": {"<immich_asset_id>": "<amazon_node_id>", ...}}


def load_state() -> dict:
    """Return dict of immich_asset_id → amazon_node_id."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            # Migrate old format (flat list of IDs) → new format
            if isinstance(data.get("synced_ids"), list):
                log.info(
                    "Migrating state from old format (synced_ids list). "
                    "Previously synced photos will be re-uploaded once."
                )
                return {}
            return data.get("synced", {})
        except Exception:
            log.warning("State file corrupt, starting fresh.")
    return {}


def save_state(synced: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"synced": synced}, indent=2))


# Amazon Drive API


def ap_get(path: str, extra_params: str = "") -> dict:
    url = _url_with_params(f"{AMAZON_DRIVE_URL}/{path.lstrip('/')}", extra_params)
    return _json_request(url, headers=AP_HEADERS)


def get_root_id() -> str:
    data = ap_get("nodes", "filters=isRoot%3Atrue")
    return data["data"][0]["id"]


def upload_to_amazon(local_path: Path, root_id: str) -> str | None:
    """Upload a file to Amazon Photos root folder.

    Returns the Amazon node ID on success, or None if the file already
    exists (409 conflict) and the response doesn't include the existing ID.
    """
    metadata = json.dumps(
        {
            "name": local_path.name,
            "kind": "FILE",
            "parents": [root_id],
        }
    ).encode()

    file_data = local_path.read_bytes()

    body, content_type = _encode_multipart(
        {
            "metadata": (None, metadata, "application/json"),
            "content": (local_path.name, file_data, "application/octet-stream"),
        }
    )

    url = _url_with_params(f"{AMAZON_CONTENT_URL}/nodes")
    headers = {**AP_HEADERS, "Content-Type": content_type}

    try:
        resp = _json_request(
            url, headers=headers, data=body, method="POST", timeout=120
        )
        node_id = resp["id"]
        log.info(f"  Uploaded: {local_path.name} → {node_id}")
        return node_id
    except HTTPError as e:
        if e.code == 409:
            log.info(f"  Already exists on Amazon: {local_path.name}")
            try:
                return json.loads(e.read()).get("id")
            except Exception:
                return None
        error_body = ""
        try:
            error_body = e.read().decode()[:200]
        except Exception:
            pass
        log.error(f"  Amazon upload error: {e.code} {error_body}")
        raise


def delete_from_amazon(node_id: str, asset_id: str) -> None:
    """Move an Amazon Photos node to trash (not permanent delete)."""
    url = _url_with_params(f"{AMAZON_DRIVE_URL}/trash/{node_id}")
    try:
        _request(url, headers=AP_HEADERS, method="PUT", data=b"")
        log.info(f"  Trashed on Amazon: {asset_id} ({node_id})")
    except HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode()[:200]
        except Exception:
            pass
        log.error(f"  Amazon delete error: {e.code} {error_body}")
        raise


# Immich API


def immich_get(path: str) -> dict | list:
    url = f"{IMMICH_URL}/api/{path.lstrip('/')}"
    return _json_request(url, headers=IMMICH_HEADERS)


def get_album_assets(album_id: str) -> list:
    return immich_get(f"albums/{album_id}").get("assets", [])


def download_asset(asset_id: str, filename: str, dest_dir: str) -> Path:
    dest = Path(dest_dir) / filename
    url = f"{IMMICH_URL}/api/assets/{asset_id}/original"
    req = Request(url, headers=IMMICH_HEADERS)
    with urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


# Main


def main() -> None:
    _configure()

    log.info("Starting Immich → Amazon Photos sync")
    synced = load_state()

    # Resolve album names → IDs
    all_albums = immich_get("albums")
    album_map = {a["albumName"]: a["id"] for a in all_albums}

    target_albums = []
    for name in ALBUMS_TO_SYNC:
        if name in album_map:
            target_albums.append((name, album_map[name]))
        else:
            log.warning(f"Album not found: {name} - skipping")

    if not target_albums:
        log.error("No matching albums found. Check IMMICH_ALBUMS.")
        sys.exit(1)

    root_id = get_root_id()
    log.info(f"Amazon Photos root node: {root_id}")

    total_uploaded = 0
    total_deleted = 0
    total_errors = 0

    # Collect all current asset IDs across target albums
    current_asset_ids: set[str] = set()

    with tempfile.TemporaryDirectory() as tmp:
        for album_name, album_id in target_albums:
            assets = get_album_assets(album_id)
            asset_ids = {a["id"] for a in assets}
            current_asset_ids.update(asset_ids)
            new_assets = [a for a in assets if a["id"] not in synced]
            log.info(
                f"Album '{album_name}': {len(assets)} total, {len(new_assets)} new"
            )

            for asset in new_assets:
                asset_id = asset["id"]
                filename = asset.get("originalFileName", f"{asset_id}.jpg")
                try:
                    local_path = download_asset(asset_id, filename, tmp)
                    node_id = upload_to_amazon(local_path, root_id)
                    local_path.unlink(missing_ok=True)
                    if node_id:
                        synced[asset_id] = node_id
                    total_uploaded += 1
                except Exception as e:
                    log.error(f"  Failed {filename}: {e}")
                    total_errors += 1

    # Delete assets that were removed from all target albums
    removed_ids = set(synced.keys()) - current_asset_ids
    if removed_ids:
        log.info(f"Deleting {len(removed_ids)} asset(s) removed from albums")
    for asset_id in removed_ids:
        node_id = synced.get(asset_id)
        if not node_id:
            log.warning(f"  No Amazon node ID for {asset_id}, removing from state")
            del synced[asset_id]
            continue
        try:
            delete_from_amazon(node_id, asset_id)
            del synced[asset_id]
            total_deleted += 1
        except Exception as e:
            log.error(f"  Failed to delete {asset_id}: {e}")
            total_errors += 1

    save_state(synced)
    log.info(
        f"Done. Uploaded: {total_uploaded}, Deleted: {total_deleted}, "
        f"Errors: {total_errors}, Tracked: {len(synced)}"
    )
    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
