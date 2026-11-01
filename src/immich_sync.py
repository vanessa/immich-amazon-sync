#!/usr/bin/env python3
"""
Immich → Amazon Photos sync.

Syncs photos from specified Immich albums into an Amazon Photos album using
the (unofficial) Amazon Drive v1 API directly. Zero external dependencies -
stdlib only.

Features:
  - One-way sync: Immich albums → Amazon Photos album (files live in root)
  - Deletion: photos removed from all Immich albums are removed from the
    Amazon album and (optionally) trashed - guarded by a deletion brake
  - State tracking: maps Immich asset IDs → Amazon node IDs in a JSON file
  - Deduplication: skips assets already synced (by ID); upload conflicts are
    resolved by MD5 so existing Amazon nodes are adopted, not duplicated
  - Region aware: amazon.de by default, any TLD via AP_TLD
"""

import gzip
import hashlib
import json
import logging
import logging.handlers
import math
import mimetypes
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, install_opener, urlopen

# Constants

AMAZON_BASE_PARAMS = "asset=ALL&tempLink=false&resourceVersion=V2&ContentType=JSON"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Upload hosts per region, taken from the amazon-photos library
# (trevorhobenshield/amazon_photos, AmazonPhotos.determine_cdproxy).
AMAZON_CONTENT_NA = "https://content-na.drive.amazonaws.com/cdproxy"
AMAZON_CONTENT_EU = "https://content-eu.drive.amazonaws.com/cdproxy"
NA_TLDS = {"com", "ca"}
EU_TLDS = {
    "at", "be", "bg", "hr", "ch", "cy", "cz", "dk", "ee", "fi", "fr", "de",
    "gr", "hu", "is", "ie", "it", "li", "lv", "lt", "lu", "mt", "no", "nl",
    "pl", "pt", "ro", "sk", "si", "es", "se", "uk", "co.uk",
}

MIN_IMMICH_VERSION = (3, 2)
IMMICH_PAGE_SIZE = 1000  # max allowed by POST /search/metadata
AMAZON_PAGE_SIZE = 200  # max nodes per GET /nodes request
AMAZON_MAX_OFFSET = 9999  # Amazon refuses offset + limit > 9999
AMAZON_BATCH_SIZE = 50  # nodes per album/trash PATCH (amazon-photos uses 50)
DOWNLOAD_CHUNK = 1024 * 1024
STATE_VERSION = 2

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DELETE_BLOCKED = 2
EXIT_AUTH = 3

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Runtime config (populated by _configure())

IMMICH_URL: str = ""
ALBUMS_TO_SYNC: list[str] = []
AP_HEADERS: dict[str, str] = {}
IMMICH_HEADERS: dict[str, str] = {}
AMAZON_DRIVE_URL: str = ""
AMAZON_CONTENT_URL: str = ""
AP_ALBUM_NAME: str = ""
DELETE_MODE: str = "trash"
DRY_RUN: bool = False
MAX_DELETE_FRACTION: float = 0.2
MAX_DELETE_COUNT: int = 50
MAX_FILE_BYTES: int = 200 * 1024 * 1024
SYNC_TYPES: set[str] = {"IMAGE"}
HEALTHCHECK_URL: str = ""
STATE_DIR: Path = Path("/data")
STATE_FILE: Path = Path("/data/state.json")

# Values that must never show up in logs (see _RedactingFormatter)
_SECRETS: set[str] = set()

log = logging.getLogger("immich_sync")


class ConfigError(Exception):
    """Invalid or missing configuration."""


class FatalError(Exception):
    """Unrecoverable problem - abort the run before touching anything."""


class SkipAsset(Exception):
    """Asset is deliberately not synced in this run (e.g. too large)."""


class AmazonError(Exception):
    """Non-2xx response from Amazon. `body` holds (a prefix of) the response."""

    def __init__(self, code: int, body: str = ""):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code}: {body[:200]}")


class AmazonAuthError(AmazonError):
    """401/403 from Amazon - cookies expired or invalid."""


# Configuration


def _env(name: str, default: str | None = None) -> str | None:
    """Read NAME, or the contents of the file named by NAME_FILE (Docker secrets)."""
    file_path = os.getenv(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ConfigError(f"cannot read {name}_FILE: {e.strerror}") from None
    value = os.getenv(name)
    return value if value else default


def _require_env(name: str) -> str:
    """Get a required setting or raise with a clear message."""
    value = _env(name)
    if not value:
        raise ConfigError(f"required environment variable {name} (or {name}_FILE) is not set")
    return value


def _secret(name: str) -> str:
    """Required setting that must never be logged and must be a safe header value."""
    value = _require_env(name)
    if any(ch in value for ch in "\r\n;") or not value.isprintable():
        raise ConfigError(f"{name} contains characters that are not allowed in a cookie/header")
    _SECRETS.add(value)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _env_number(name: str, default: float, cast=float):
    value = _env(name)
    if value is None:
        return default
    try:
        number = cast(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if not math.isfinite(number) or number < 0:
        raise ConfigError(f"{name} must be a finite, non-negative number")
    return number


def _http_url(name: str, value: str, *, https_only: bool = False) -> str:
    parts = urlsplit(value)
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme not in allowed or not parts.netloc:
        raise ConfigError(f"{name} must be an {' or '.join(allowed)} URL")
    return value.rstrip("/")


def region_settings(tld: str) -> dict[str, str | None]:
    """Derive Amazon hosts and cookie names from a TLD such as 'de' or 'co.uk'."""
    tld = tld.strip().lower().lstrip(".")
    if not re.fullmatch(r"[a-z]{2,3}(\.[a-z]{2,3})?", tld):
        raise ConfigError(f"AP_TLD '{tld}' is not a valid Amazon top-level domain")
    # amazon.com uses "-main", every other marketplace "-acb<country>"
    # (e.g. amazon.de → ubid-acbde / at-acbde, amazon.co.uk → ubid-acbuk / at-acbuk)
    suffix = "main" if tld == "com" else "acb" + tld.rsplit(".", 1)[-1]
    if tld in NA_TLDS:
        content_url = AMAZON_CONTENT_NA
    elif tld in EU_TLDS:
        content_url = AMAZON_CONTENT_EU
    else:
        content_url = None
    return {
        "drive_url": f"https://www.amazon.{tld}/drive/v1",
        "content_url": content_url,
        "ubid_key": f"ubid-{suffix}",
        "at_key": f"at-{suffix}",
    }


def _configure() -> None:
    """Read env vars and set up module-level config. Called once from main()."""
    global IMMICH_URL, ALBUMS_TO_SYNC, AP_HEADERS, IMMICH_HEADERS
    global AMAZON_DRIVE_URL, AMAZON_CONTENT_URL, AP_ALBUM_NAME, DELETE_MODE, DRY_RUN
    global MAX_DELETE_FRACTION, MAX_DELETE_COUNT, MAX_FILE_BYTES, SYNC_TYPES
    global HEALTHCHECK_URL, STATE_DIR, STATE_FILE

    _SECRETS.clear()

    IMMICH_URL = _http_url("IMMICH_URL", _require_env("IMMICH_URL"))
    immich_api_key = _secret("IMMICH_API_KEY")
    ALBUMS_TO_SYNC = [
        a.strip() for a in _require_env("IMMICH_ALBUMS").split(",") if a.strip()
    ]
    if not ALBUMS_TO_SYNC:
        raise ConfigError("IMMICH_ALBUMS does not contain any album name")

    region = region_settings(_env("AP_TLD", "de"))
    AMAZON_DRIVE_URL = region["drive_url"]
    content_override = _env("AP_CONTENT_URL")
    if content_override:
        content_override = _http_url("AP_CONTENT_URL", content_override, https_only=True)
        AMAZON_CONTENT_URL = re.sub(r"/nodes$", "", content_override)
    elif region["content_url"]:
        AMAZON_CONTENT_URL = region["content_url"]
    else:
        raise ConfigError(
            f"no known upload host for AP_TLD={_env('AP_TLD')}; set AP_CONTENT_URL"
        )

    ubid_key = _env("AP_UBID_KEY", region["ubid_key"])
    at_key = _env("AP_AT_KEY", region["at_key"])
    for key_name, key in (("AP_UBID_KEY", ubid_key), ("AP_AT_KEY", at_key)):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ConfigError(f"{key_name} is not a valid cookie name")

    session_id = _secret("AP_SESSION_ID")
    ubid_value = _secret("AP_UBID_VALUE")
    at_value = _secret("AP_AT_VALUE")
    user_agent = _env("AP_USER_AGENT", DEFAULT_USER_AGENT)
    if not user_agent.isprintable():
        raise ConfigError("AP_USER_AGENT contains control characters")

    AP_HEADERS = {
        "Cookie": f"session-id={session_id}; {ubid_key}={ubid_value}; {at_key}={at_value}",
        "x-amzn-sessionid": session_id,
        "User-Agent": user_agent,
    }
    IMMICH_HEADERS = {"x-api-key": immich_api_key, "Accept": "application/json"}

    AP_ALBUM_NAME = _require_env("AP_ALBUM_NAME").strip()
    DELETE_MODE = _env("AP_DELETE_MODE", "trash").lower()
    if DELETE_MODE not in ("trash", "album_only"):
        raise ConfigError("AP_DELETE_MODE must be 'trash' or 'album_only'")

    DRY_RUN = _env_bool("DRY_RUN")
    MAX_DELETE_FRACTION = _env_number("MAX_DELETE_FRACTION", 0.2)
    MAX_DELETE_COUNT = _env_number("MAX_DELETE_COUNT", 50, int)
    MAX_FILE_BYTES = int(_env_number("MAX_FILE_MB", 200) * 1024 * 1024)

    SYNC_TYPES = {t.strip().upper() for t in _env("SYNC_TYPES", "IMAGE").split(",") if t.strip()}
    if not SYNC_TYPES or not SYNC_TYPES <= {"IMAGE", "VIDEO"}:
        raise ConfigError("SYNC_TYPES must be IMAGE, VIDEO or IMAGE,VIDEO")

    HEALTHCHECK_URL = _env("HEALTHCHECK_URL", "")
    if HEALTHCHECK_URL:
        HEALTHCHECK_URL = _http_url("HEALTHCHECK_URL", HEALTHCHECK_URL)
        _SECRETS.add(HEALTHCHECK_URL)  # ping URLs usually embed a token

    STATE_DIR = Path(_env("STATE_DIR", "/data"))
    STATE_FILE = STATE_DIR / "state.json"


# Logging


class _RedactingFormatter(logging.Formatter):
    """Formatter that masks every configured secret, including in tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in _SECRETS:
            if len(secret) >= 4:
                text = text.replace(secret, "***")
        return text


def _setup_logging() -> None:
    _teardown_logging()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_file = STATE_DIR / "immich-sync.log"
    # create the log file 0600 before the handler opens it
    os.close(os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    os.chmod(log_file, 0o600)
    formatter = _RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s")
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _teardown_logging() -> None:
    for handler in list(log.handlers):
        if isinstance(handler.formatter, _RedactingFormatter):
            log.removeHandler(handler)
            handler.close()


# HTTP helpers


class _NoRedirect(HTTPRedirectHandler):
    """Never follow redirects: an expired Amazon session redirects to the sign-in
    page, and following it would forward the Cookie header to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


install_opener(build_opener(_NoRedirect))


def _url_with_params(base: str, extra_params: str = "") -> str:
    params = AMAZON_BASE_PARAMS
    if extra_params:
        params += f"&{extra_params}"
    return f"{base}?{params}"


def _request(
    url: str,
    *,
    headers: dict | None = None,
    data=None,
    method: str | None = None,
    timeout: int = 30,
) -> bytes:
    """Make an HTTP request and return the response body. Raises on non-2xx."""
    req = Request(url, data=data, headers=headers or {}, method=method)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
    # amazon.de answers some writes (PATCH .../children) gzip-compressed without being asked to,
    # and urllib does not decode that. The request itself succeeded; only reading the reply failed.
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body


def _json_request(
    url: str,
    *,
    headers: dict | None = None,
    data=None,
    method: str | None = None,
    timeout: int = 30,
) -> dict:
    body = _request(url, headers=headers, data=data, method=method, timeout=timeout)
    return json.loads(body) if body.strip() else {}


def _error_body(e: HTTPError, limit: int = 4096) -> str:
    try:
        return e.read(limit).decode("utf-8", "replace")
    except Exception:
        return ""


# Streaming multipart form-data encoder


class _MultipartStream:
    """File-like multipart body that streams file parts instead of loading them."""

    def __init__(self, parts: list):
        self._parts = parts
        self._length = 0
        for part in parts:
            if isinstance(part, bytes):
                self._length += len(part)
            else:
                self._length += os.fstat(part.fileno()).st_size - part.tell()

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        out = bytearray()
        while self._parts and (size < 0 or len(out) < size):
            part = self._parts[0]
            want = -1 if size < 0 else size - len(out)
            if isinstance(part, bytes):
                chunk = part if want < 0 else part[:want]
                rest = part[len(chunk):]
                if rest:
                    self._parts[0] = rest
                else:
                    self._parts.pop(0)
            else:
                chunk = part.read(want)
                if not chunk:
                    self._parts.pop(0)
                    continue
            out += chunk
        return bytes(out)


def _encode_multipart(fields: dict) -> tuple[_MultipartStream, str]:
    """Encode fields as a streaming multipart/form-data body.

    Each field value is a tuple of (filename | None, bytes | binary file, content_type).
    Returns (body_stream, content_type_header); len(body_stream) is the exact size.
    """
    boundary = f"----FormBoundary{uuid.uuid4().hex[:16]}"
    parts: list = []

    for field_name, (filename, data, content_type) in fields.items():
        disposition = f'form-data; name="{field_name}"'
        if filename:
            disposition += f'; filename="{filename}"'

        parts.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: {disposition}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"\r\n"
            ).encode()
        )
        parts.append(data)
        parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    return _MultipartStream(parts), f"multipart/form-data; boundary={boundary}"


# File names


def safe_filename(raw: str | None, asset_id: str, mime_type: str | None = None) -> str:
    """Turn an untrusted Immich file name into a harmless display name.

    Strips directories, control characters and quotes. Falls back to
    <asset_id>.<ext> when nothing usable is left.
    """
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable() and ch != '"').strip()
    if name in ("", ".", ".."):
        ext = ""
        if raw:
            ext = PurePosixPath(raw.replace("\\", "/")).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext or ""):
            ext = mimetypes.guess_extension(mime_type or "") or ".jpg"
        return f"{asset_id}{ext}"
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem[:190]}.{ext[:9]}" if dot and len(ext) <= 9 else name[:200]
    return name


def _disambiguated_name(name: str, asset_id: str) -> str:
    path = PurePosixPath(name)
    return f"{path.stem}_{asset_id[:8]}{path.suffix}"


# State
# state.json schema (v2):
# {"version": 2, "album_node_id": "<amazon album id>" | null,
#  "synced": {"<immich_asset_id>": "<amazon_node_id>", ...},
#  "pending_album_add": ["<amazon_node_id>", ...]}


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "album_node_id": None, "synced": {},
            "pending_album_add": [], "adopted_nodes": []}


def load_state() -> dict:
    """Load the state file, migrating older formats. Raises FatalError if unreadable."""
    if not STATE_FILE.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        raise FatalError(
            f"State file {STATE_FILE} is unreadable ({e}). Fix or remove it - "
            "refusing to continue so tracked photos are not lost."
        ) from None

    version = data.get("version", 1)
    if version == STATE_VERSION:
        state = _empty_state()
        state.update(
            album_node_id=data.get("album_node_id"),
            synced=dict(data.get("synced") or {}),
            pending_album_add=list(data.get("pending_album_add") or []),
            adopted_nodes=list(data.get("adopted_nodes") or []),
        )
        return state
    if version != 1:
        raise FatalError(f"State file version {version} is newer than this tool supports.")

    state = _empty_state()
    if isinstance(data.get("synced_ids"), list):
        log.info(
            "Migrating state from old format (synced_ids list). "
            "Previously synced photos will be matched again by upload conflict."
        )
        return state
    state["synced"] = dict(data.get("synced") or {})
    log.info(f"Migrating state v1 → v2 ({len(state['synced'])} tracked assets, no re-upload)")
    return state


def save_state(state: dict) -> None:
    """Atomically write the state file (0600). No-op in dry-run mode."""
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, STATE_FILE)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# Amazon Drive API


def _amazon_call(url: str, *, data=None, method: str | None = None,
                 headers: dict | None = None, timeout: int = 30) -> bytes:
    try:
        return _request(url, headers={**AP_HEADERS, **(headers or {})},
                        data=data, method=method, timeout=timeout)
    except HTTPError as e:
        body = _error_body(e)
        if e.code in (401, 403) or 300 <= e.code < 400:
            # 3xx: Amazon sends expired sessions to the sign-in page
            raise AmazonAuthError(e.code, body) from None
        raise AmazonError(e.code, body) from None


def ap_get(path: str, extra_params: str = "") -> dict:
    url = _url_with_params(f"{AMAZON_DRIVE_URL}/{path.lstrip('/')}", extra_params)
    raw = _amazon_call(url)
    return json.loads(raw) if raw.strip() else {}


def ap_write(method: str, path: str, body: dict) -> dict:
    """JSON write request, shaped like the amazon-photos library sends it."""
    payload = json.dumps({**body, "resourceVersion": "V2", "ContentType": "JSON"}).encode()
    raw = _amazon_call(
        f"{AMAZON_DRIVE_URL}/{path.lstrip('/')}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    return json.loads(raw) if raw.strip() else {}


def list_nodes(filters: str) -> list[dict]:
    """GET /nodes with an Amazon filter expression, following offset pagination."""
    nodes: list[dict] = []
    offset = 0
    while offset < AMAZON_MAX_OFFSET:
        limit = min(AMAZON_PAGE_SIZE, AMAZON_MAX_OFFSET - offset)
        page = ap_get("nodes", f"filters={quote(filters, safe='')}&limit={limit}&offset={offset}")
        data = page.get("data") or []
        nodes.extend(data)
        offset += len(data)
        if not data or offset >= int(page.get("count") or 0):
            break
    return nodes


def _escape_filter(value: str) -> str:
    return "".join("\\" + ch if ch in '+-&|!(){}[]^"~*?:\\/\' ' else ch for ch in value)


def get_root_id() -> str:
    data = ap_get("nodes", "filters=isRoot%3Atrue")
    return data["data"][0]["id"]


def find_or_create_album(name: str, known_id: str | None) -> str | None:
    """Return the node ID of the Amazon Photos album called `name`, creating it if needed.

    Returns None only in dry-run mode when the album would have to be created.
    """
    if known_id:
        try:
            node = ap_get(f"nodes/{quote(known_id, safe='')}")
        except AmazonAuthError:
            raise
        except AmazonError as e:
            if e.code != 404:
                raise
            node = {}
        if (node.get("kind") == "VISUAL_COLLECTION" and node.get("status", "AVAILABLE") == "AVAILABLE"
                and node.get("name") == name):
            return known_id
        log.info(f"Stored album node {known_id} is gone or renamed - looking up '{name}'")

    matches = [
        n for n in list_nodes("kind:VISUAL_COLLECTION")
        if n.get("name") == name and n.get("status", "AVAILABLE") == "AVAILABLE"
    ]
    if matches:
        matches.sort(key=lambda n: n.get("createdDate", ""))
        if len(matches) > 1:
            log.warning(f"{len(matches)} Amazon albums are named '{name}' - using the oldest")
        return matches[0]["id"]

    if DRY_RUN:
        log.info(f"[dry-run] Would create Amazon Photos album '{name}'")
        return None
    created = ap_write("POST", "nodes", {"kind": "VISUAL_COLLECTION", "name": name})
    log.info(f"Created Amazon Photos album '{name}' → {created['id']}")
    return created["id"]


def album_children(album_id: str, op: str, node_ids: list[str]) -> None:
    ap_write("PATCH", f"nodes/{quote(album_id, safe='')}/children", {"op": op, "value": node_ids})


def trash_nodes(node_ids: list[str]) -> None:
    ap_write("PATCH", "trash", {
        "recurse": "true",
        "op": "add",
        "filters": "",
        "conflictResolution": "RENAME",
        "value": node_ids,
    })


def _apply_in_batches(action, node_ids: list[str], what: str) -> tuple[set, set, set]:
    """Run `action(batch)` in batches; on failure retry nodes one by one.

    Returns (done, gone, failed) node ID sets. "gone" means Amazon answered 404.
    """
    done: set[str] = set()
    gone: set[str] = set()
    failed: set[str] = set()
    for i in range(0, len(node_ids), AMAZON_BATCH_SIZE):
        batch = node_ids[i:i + AMAZON_BATCH_SIZE]
        try:
            action(batch)
            done.update(batch)
            continue
        except AmazonAuthError:
            raise
        except Exception as e:
            if len(batch) > 1:
                log.warning(f"  {what} failed for a batch of {len(batch)} ({e}); retrying one by one")
        for node_id in batch:
            try:
                action([node_id])
                done.add(node_id)
            except AmazonAuthError:
                raise
            except AmazonError as e:
                if e.code == 404:
                    gone.add(node_id)
                else:
                    log.error(f"  {what} failed for node {node_id}: {e}")
                    failed.add(node_id)
            except Exception as e:
                log.error(f"  {what} failed for node {node_id}: {e}")
                failed.add(node_id)
    return done, gone, failed


def upload_to_amazon(local_path: Path, name: str, root_id: str) -> str:
    """Stream a file into the Amazon Photos root folder and return its node ID.

    Raises AmazonError (code 409 on name/content conflict).
    """
    metadata = json.dumps({"name": name, "kind": "FILE", "parents": [root_id]}).encode()
    with open(local_path, "rb") as f:
        body, content_type = _encode_multipart(
            {
                "metadata": (None, metadata, "application/json"),
                "content": (name, f, "application/octet-stream"),
            }
        )
        raw = _amazon_call(
            _url_with_params(f"{AMAZON_CONTENT_URL}/nodes"),
            data=body,
            method="POST",
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
            timeout=120,
        )
    return json.loads(raw)["id"]


def _conflict_node_id(body: str) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    if isinstance(data, dict):
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        for candidate in (data.get("id"), info.get("nodeId")):
            if isinstance(candidate, str) and candidate:
                return candidate
    match = re.search(r"NodeId:?\s*([A-Za-z0-9_-]{8,})", body)
    return match.group(1) if match else None


def find_existing_node(conflict_body: str, name: str, md5: str, root_id: str) -> str | None:
    """After a 409, find an AVAILABLE node with identical content (MD5)."""
    candidates: list[dict] = []
    node_id = _conflict_node_id(conflict_body)
    if node_id:
        try:
            candidates.append(ap_get(f"nodes/{quote(node_id, safe='')}"))
        except AmazonAuthError:
            raise
        except AmazonError:
            pass
    for label, filters in (
        ("md5", f"kind:FILE AND contentProperties.md5:{md5}"),
        ("name", f"kind:FILE AND parents:{root_id} AND name:{_escape_filter(name)}"),
    ):
        try:
            candidates.extend(list_nodes(filters))
        except AmazonAuthError:
            raise
        except AmazonError as e:
            log.info(f"  Conflict lookup by {label} failed: {e}")
    for node in candidates:
        props = node.get("contentProperties") or {}
        if (props.get("md5", "").lower() == md5 and node.get("status", "AVAILABLE") == "AVAILABLE"
                and node.get("id")):
            return node["id"]
    return None


# Immich API


def immich_request(path: str, body: dict | None = None) -> dict | list:
    url = f"{IMMICH_URL}/api/{path.lstrip('/')}"
    if body is None:
        return _json_request(url, headers=IMMICH_HEADERS)
    return _json_request(
        url,
        headers={**IMMICH_HEADERS, "Content-Type": "application/json"},
        data=json.dumps(body).encode(),
        method="POST",
    )


def immich_get(path: str) -> dict | list:
    return immich_request(path)


def check_immich_version() -> str:
    v = immich_get("server/version")
    version = (int(v.get("major", 0)), int(v.get("minor", 0)))
    text = f"{v.get('major')}.{v.get('minor')}.{v.get('patch')}"
    if version < MIN_IMMICH_VERSION:
        raise FatalError(
            f"Immich {text} is too old - this tool needs Immich >= "
            f"{MIN_IMMICH_VERSION[0]}.{MIN_IMMICH_VERSION[1]} (cursor-based album search)."
        )
    return text


def search_album_assets(album_id: str) -> list[dict]:
    """All non-trashed, visible assets of an album via POST /search/metadata.

    Uses the structured search shape (Immich >= 3.2): flat `albumIds`/`page`
    are deprecated and cannot be combined with `cursor`.
    `visibility in [archive, timeline]` mirrors the album's assetCount.
    """
    body: dict = {
        "filter": {
            "albumIds": {"any": [album_id]},
            "visibility": {"in": ["archive", "timeline"]},
            "trashedAt": {"eq": None},
        },
        "size": IMMICH_PAGE_SIZE,
    }
    items: list[dict] = []
    seen_cursors: set[str] = set()
    while True:
        resp = immich_request("search/metadata", body)
        assets = resp.get("assets") if isinstance(resp, dict) else None
        if not isinstance(assets, dict) or "nextCursor" not in assets:
            raise FatalError(
                "Immich search response has no 'nextCursor' - Immich >= 3.2 is required."
            )
        page = assets.get("items")
        if not isinstance(page, list):
            raise ValueError("Immich search response has no 'items' list")
        items.extend(page)
        cursor = assets["nextCursor"]
        if not cursor:
            return items
        if cursor in seen_cursors:
            raise ValueError("Immich returned the same search cursor twice")
        seen_cursors.add(cursor)
        body = {**body, "cursor": cursor}


def get_album_asset_count(album_id: str) -> int:
    return int(immich_get(f"albums/{album_id}")["assetCount"])


def download_asset(asset_id: str, dest: Path) -> tuple[str, int]:
    """Stream an original into `dest`. Returns (md5 hex, size). Enforces MAX_FILE_MB."""
    url = f"{IMMICH_URL}/api/assets/{asset_id}/original"
    req = Request(url, headers=IMMICH_HEADERS)
    limit_mb = MAX_FILE_BYTES / 1024 / 1024
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with urlopen(req, timeout=60) as resp:
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_FILE_BYTES:
            raise SkipAsset(f"{int(length) / 1024 / 1024:.0f} MB exceeds MAX_FILE_MB={limit_mb:g}")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            while chunk := resp.read(DOWNLOAD_CHUNK):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise SkipAsset(f"exceeds MAX_FILE_MB={limit_mb:g}")
                md5.update(chunk)
                f.write(chunk)
    return md5.hexdigest(), size


def sync_asset(asset: dict, name: str, tmp_dir: str, root_id: str) -> tuple[str, str]:
    """Download one asset and upload it. Returns (node_id, how) with how in uploaded/adopted."""
    asset_id = asset["id"]
    local_path = Path(tmp_dir) / asset_id  # asset_id is a validated UUID
    try:
        md5, _ = download_asset(asset_id, local_path)
        upload_name = name
        for attempt in range(2):
            try:
                return upload_to_amazon(local_path, upload_name, root_id), "uploaded"
            except AmazonError as e:
                if isinstance(e, AmazonAuthError) or e.code != 409:
                    raise
                existing = find_existing_node(e.body, upload_name, md5, root_id)
                if existing:
                    return existing, "adopted"
                if attempt == 0:
                    upload_name = _disambiguated_name(name, asset_id)
                    log.info(f"  Name conflict with different content, retrying as {upload_name}")
        raise AmazonError(409, "conflict persists after renaming")
    finally:
        local_path.unlink(missing_ok=True)


def ping_healthcheck() -> None:
    if not HEALTHCHECK_URL:
        return
    try:
        _request(HEALTHCHECK_URL, headers={"User-Agent": "immich-amazon-sync"}, timeout=10)
    except Exception as e:
        log.warning(f"Healthcheck ping failed: {type(e).__name__}")


# Main


def flush_pending_album_adds(state: dict) -> int:
    """Add uploaded-but-not-yet-linked nodes to the album. Returns number of failures."""
    pending = list(dict.fromkeys(state["pending_album_add"]))  # de-duplicate, keep order
    album_id = state["album_node_id"]
    if not pending:
        return 0
    if DRY_RUN or not album_id:
        log.info(f"[dry-run] Would add {len(pending)} node(s) to the Amazon album")
        return 0
    done, gone, failed = _apply_in_batches(
        lambda ids: album_children(album_id, "add", ids), pending, "Add to album"
    )
    if gone:
        # node no longer exists on Amazon → forget it so the asset is uploaded again
        log.warning(f"  {len(gone)} node(s) no longer exist on Amazon; will re-upload")
        state["synced"] = {a: n for a, n in state["synced"].items() if n not in gone}
    if done:
        log.info(f"  Added {len(done)} node(s) to album")
    state["pending_album_add"] = [n for n in pending if n in failed]
    save_state(state)
    return len(failed)


def _resolve_albums() -> tuple[list[tuple[str, str]], bool]:
    """Map IMMICH_ALBUMS names to IDs. Returns (albums, all_found)."""
    by_name: dict[str, list[str]] = {}
    for album in immich_get("albums"):
        by_name.setdefault(album["albumName"], []).append(album["id"])

    targets: list[tuple[str, str]] = []
    all_found = True
    for name in ALBUMS_TO_SYNC:
        ids = by_name.get(name, [])
        if len(ids) == 1 and UUID_RE.fullmatch(ids[0]):
            targets.append((name, ids[0]))
        elif not ids:
            log.error(f"Album not found in Immich: '{name}'")
            all_found = False
        else:
            log.error(f"Album name '{name}' is ambiguous or invalid in Immich ({len(ids)} matches)")
            all_found = False
    return targets, all_found


def run() -> int:
    log.info("Starting Immich → Amazon Photos sync" + (" [DRY RUN]" if DRY_RUN else ""))
    state = load_state()
    synced: dict[str, str] = state["synced"]

    log.info(f"Immich server version {check_immich_version()}")
    target_albums, immich_complete = _resolve_albums()
    if not target_albums:
        log.error("No matching albums found. Check IMMICH_ALBUMS.")
        return EXIT_ERROR

    root_id = get_root_id()
    log.info(f"Amazon Photos root node: {root_id}")

    album_id = find_or_create_album(AP_ALBUM_NAME, state["album_node_id"])
    log.info(f"Amazon Photos album '{AP_ALBUM_NAME}': {album_id or '(to be created)'}")
    if album_id != state["album_node_id"]:
        if synced:
            log.info(f"Album is new to this state - linking {len(synced)} tracked photo(s)")
            pending = set(state["pending_album_add"])
            state["pending_album_add"] += [n for n in synced.values() if n not in pending]
        state["album_node_id"] = album_id
        save_state(state)

    errors = flush_pending_album_adds(state)
    synced = state["synced"]

    # Collect all current assets across target albums
    present_ids: set[str] = set()
    wanted: dict[str, dict] = {}
    for album_name, immich_album_id in target_albums:
        try:
            assets = search_album_assets(immich_album_id)
            expected = get_album_asset_count(immich_album_id)
        except FatalError:
            raise
        except Exception as e:
            log.error(f"Immich query for album '{album_name}' failed: {e}")
            immich_complete = False
            errors += 1
            continue

        unique = {}
        for a in assets:
            if not isinstance(a.get("id"), str) or not UUID_RE.fullmatch(a["id"]):
                log.error(f"Album '{album_name}': ignoring asset with invalid id")
                immich_complete = False
                continue
            unique[a["id"]] = a
        if len(unique) != expected:
            log.warning(
                f"Album '{album_name}': search returned {len(unique)} assets but album "
                f"reports assetCount={expected} - no deletions this run"
            )
            immich_complete = False

        album_wanted = 0
        for asset_id, a in unique.items():
            if a.get("isTrashed"):
                continue
            present_ids.add(asset_id)
            if a.get("type") in SYNC_TYPES:
                wanted[asset_id] = a
                album_wanted += 1
        log.info(f"Album '{album_name}': {len(unique)} assets, {album_wanted} of type "
                 f"{'/'.join(sorted(SYNC_TYPES))}")

    new_assets = [a for asset_id, a in wanted.items() if asset_id not in synced]
    log.info(f"{len(wanted)} assets to mirror, {len(new_assets)} new")

    tracked_before = len(synced)  # deletion brake base, excluding this run's uploads
    total_uploaded = 0
    total_skipped = 0
    with tempfile.TemporaryDirectory(prefix="immich-sync-") as tmp:
        for asset in new_assets:
            asset_id = asset["id"]
            name = safe_filename(asset.get("originalFileName"), asset_id, asset.get("originalMimeType"))
            if DRY_RUN:
                log.info(f"  [dry-run] Would upload {name} ({asset_id})")
                continue
            try:
                node_id, how = sync_asset(asset, name, tmp, root_id)
            except AmazonAuthError:
                raise
            except SkipAsset as e:
                log.warning(f"  Skipped {name}: {e}")
                total_skipped += 1
                continue
            except Exception as e:
                log.error(f"  Failed {name}: {e}")
                errors += 1
                continue
            log.info(f"  {how.capitalize()}: {name} → {node_id}")
            synced[asset_id] = node_id
            state["pending_album_add"].append(node_id)
            if how == "adopted" and node_id not in state["adopted_nodes"]:
                # not uploaded by us (could be the user's own backup): never trash it
                state["adopted_nodes"].append(node_id)
            save_state(state)
            total_uploaded += 1
            if len(state["pending_album_add"]) >= AMAZON_BATCH_SIZE:
                errors += flush_pending_album_adds(state)
                synced = state["synced"]
        errors += flush_pending_album_adds(state)
        synced = state["synced"]

    # Delete assets that were removed from all target albums
    removed = sorted(set(synced) - present_ids)
    total_deleted = 0
    delete_blocked = False
    if removed:
        fraction = len(removed) / max(tracked_before, 1)
        if not immich_complete:
            log.error(f"Not deleting {len(removed)} asset(s): Immich data incomplete in this run")
            delete_blocked = True
        elif len(removed) > MAX_DELETE_COUNT or fraction > MAX_DELETE_FRACTION:
            log.error(
                f"Deletion brake: {len(removed)} of {tracked_before} tracked assets "
                f"({fraction:.0%}) would be deleted (limits: MAX_DELETE_COUNT={MAX_DELETE_COUNT}, "
                f"MAX_DELETE_FRACTION={MAX_DELETE_FRACTION:g}). Nothing deleted."
            )
            delete_blocked = True
        elif DRY_RUN:
            for asset_id in removed:
                log.info(f"  [dry-run] Would remove {asset_id} ({synced[asset_id]}) "
                         f"from album{' and trash it' if DELETE_MODE == 'trash' else ''}")
        else:
            total_deleted, delete_errors = delete_assets(state, removed)
            errors += delete_errors
            synced = state["synced"]

    log.info(
        f"Done. Uploaded: {total_uploaded}, Deleted: {total_deleted}, Skipped: {total_skipped}, "
        f"Errors: {errors}, Tracked: {len(synced)}" + (" [DRY RUN - nothing written]" if DRY_RUN else "")
    )
    if delete_blocked:
        return EXIT_DELETE_BLOCKED
    if errors or not immich_complete:
        return EXIT_ERROR
    return EXIT_OK


def _is_trashed(node_id: str) -> bool:
    try:
        return ap_get(f"nodes/{quote(node_id, safe='')}").get("status") == "TRASH"
    except AmazonAuthError:
        raise
    except Exception:
        return False


def delete_assets(state: dict, asset_ids: list[str]) -> tuple[int, int]:
    """Remove assets' nodes from the album (and trash them). Returns (deleted, errors).

    Nodes adopted from an existing Amazon file are only removed from the album,
    never trashed. State is saved after every batch.
    """
    synced = state["synced"]
    album_id = state["album_node_id"]
    leaving = set(asset_ids)
    # identical content can map two Immich assets to one adopted node - keep shared nodes
    still_used = {n for a, n in synced.items() if a not in leaving}
    node_to_assets: dict[str, list[str]] = {}
    for asset_id in asset_ids:
        if synced[asset_id] in still_used:
            log.info(f"  Forgetting {asset_id}: its node is still used by another asset")
            del synced[asset_id]
        else:
            node_to_assets.setdefault(synced[asset_id], []).append(asset_id)
    node_ids = list(node_to_assets)
    log.info(f"Removing {len(node_ids)} asset(s) that left all Immich albums")

    adopted = set(state["adopted_nodes"])
    total_finished = 0
    total_failed = 0
    for i in range(0, len(node_ids), AMAZON_BATCH_SIZE):
        batch = node_ids[i:i + AMAZON_BATCH_SIZE]
        # nodes still waiting for "add to album" are not in the album yet
        pending = set(state["pending_album_add"])
        in_album = [n for n in batch if n not in pending]
        done, gone, failed = _apply_in_batches(
            lambda ids: album_children(album_id, "remove", ids), in_album, "Remove from album"
        )
        removed_ok = [n for n in batch if n in done or n in gone or n in pending]

        finished = {n for n in removed_ok if DELETE_MODE != "trash" or n in adopted}
        to_trash = [n for n in removed_ok if n not in finished]
        if to_trash:
            t_done, t_gone, t_failed = _apply_in_batches(trash_nodes, to_trash, "Trash")
            already = {n for n in t_failed if _is_trashed(n)}  # e.g. trashed by an interrupted run
            finished |= t_done | t_gone | already
            failed |= t_failed - already

        for node_id in batch:
            if node_id not in finished:
                continue
            action = "Trashed" if node_id in to_trash else "Removed from album"
            for asset_id in node_to_assets[node_id]:
                del synced[asset_id]
                log.info(f"  {action}: {asset_id} ({node_id})")
        state["pending_album_add"] = [n for n in state["pending_album_add"] if n not in finished]
        state["adopted_nodes"] = [n for n in state["adopted_nodes"] if n not in finished]
        save_state(state)
        total_finished += len(finished)
        total_failed += len(failed)
    save_state(state)
    return total_finished, total_failed


def main() -> int:
    try:
        _configure()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_ERROR

    _setup_logging()
    try:
        code = run()
    except AmazonAuthError as e:
        log.error(
            f"Amazon rejected the request (HTTP {e.code}): cookies expired or invalid. "
            "Copy fresh cookies from the browser and restart. Run aborted."
        )
        code = EXIT_AUTH
    except FatalError as e:
        log.error(str(e))
        code = EXIT_ERROR
    except Exception as e:
        log.error(f"Sync failed: {type(e).__name__}: {e}")
        code = EXIT_ERROR
    else:
        if code == EXIT_OK and not DRY_RUN:
            ping_healthcheck()
    finally:
        _teardown_logging()
    return code


if __name__ == "__main__":
    sys.exit(main())
