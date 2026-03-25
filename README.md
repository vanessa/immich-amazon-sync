# immich-amazon-sync

One-way sync from [Immich](https://immich.app) albums to [Amazon Photos](https://www.amazon.com/photos).

Built to display self-hosted Immich photos on an **Amazon Echo Show** screensaver - since the Echo Show only supports Amazon Photos as a photo source, and Amazon Photos has no public API.

> Important: This is a personal project that works on my specific setup. It's not a polished, general-purpose tool. You're welcome to use it, open a PR, fork it, or learn from it, but expect to read the code and adapt things to your environment. No guarantees it'll work out of the box for you.

## How it works

```
Immich albums → (download originals) → Amazon Drive v1 API → Amazon Photos
```

- Syncs photos from one or more Immich albums to your Amazon Photos root folder
- Tracks which Immich assets map to which Amazon Photos node IDs
- Photos removed from Immich albums are moved to Amazon Photos trash
- Runs on a configurable interval (default: every 6 hours)

## Requirements

- An Immich instance with API access
- An Amazon Prime account (Amazon Photos requires Prime)
- Docker

## Setup

Create a directory and an `.env` file with your configuration (see [Configuration](#configuration) for all variables):

```bash
mkdir immich-amazon-sync && cd immich-amazon-sync
```

### Getting Amazon Photos cookies

Amazon Photos has no official API. This tool authenticates using browser cookies, which expire periodically (typically weeks to months) and must be refreshed manually.

1. Log in to [Amazon Photos](https://www.amazon.com/photos) in your browser
2. Open DevTools (F12) → **Application** tab → **Cookies** → `https://www.amazon.com`
3. Copy these three cookie values into `.env`:
   - `session-id` → `AP_SESSION_ID`
   - `ubid-main` → `AP_UBID_VALUE` (key name varies by region - see below)
   - `at-main` → `AP_AT_VALUE` (key name varies by region - see below)

> **Note:** The `at-*` cookie is `HttpOnly` and may not appear in the Application tab. In that case, go to **Network** tab → click any request to `amazon.com` → look at the `Cookie` request header to find the value.

#### Regional cookie key names

The `ubid` and `at` cookie keys include a region suffix:

| Region | `AP_UBID_KEY` | `AP_AT_KEY` |
|--------|---------------|-------------|
| US     | `ubid-main`   | `at-main`   |
| Brazil | `ubid-acbbr`  | `at-acbbr`  |
| UK     | `ubid-acbuk`  | `at-acbuk`  |
| Japan  | `ubid-acbjp`  | `at-acbjp`  |

Set `AP_UBID_KEY` and `AP_AT_KEY` in your `.env` to match your region.

### Getting an Immich API key

1. Open your Immich instance → User Settings → API Keys
2. Create a new key and copy it to `IMMICH_API_KEY` in `.env`

## Usage

```bash
docker run -d \
  --name immich-sync \
  --restart unless-stopped \
  --env-file .env \
  -e SYNC_INTERVAL_HOURS=6 \
  -v ./data:/data \
  ghcr.io/vanessa/immich-amazon-sync:main
```

View logs:

```bash
docker logs -f immich-sync
```

Force a sync now:

```bash
docker restart immich-sync
```

Full re-sync (re-uploads everything):

```bash
rm data/state.json
docker restart immich-sync
```

<details>
<summary>Docker Compose</summary>

```yaml
services:
  immich-sync:
    image: ghcr.io/vanessa/immich-amazon-sync:main
    container_name: immich-sync
    restart: unless-stopped
    env_file: .env
    environment:
      - SYNC_INTERVAL_HOURS=6
    volumes:
      - ./data:/data
```

</details>

### Configuration

All configuration is via environment variables in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `IMMICH_URL` | Immich server URL | - |
| `IMMICH_API_KEY` | Immich API key | - |
| `IMMICH_ALBUMS` | Comma-separated album names (exact match) | - |
| `AP_SESSION_ID` | Amazon `session-id` cookie | - |
| `AP_UBID_KEY` | Name of the `ubid` cookie (e.g. `ubid-main`) | - |
| `AP_UBID_VALUE` | Value of the `ubid` cookie | - |
| `AP_AT_KEY` | Name of the `at` cookie (e.g. `at-main`) | - |
| `AP_AT_VALUE` | Value of the `at` cookie | - |
| `SYNC_INTERVAL_HOURS` | Hours between sync runs | `6` |

## State

Sync state is persisted in `data/state.json`, which maps Immich asset IDs to Amazon Photos node IDs:

```json
{
  "synced": {
    "immich-asset-uuid": "amazon-node-id",
    ...
  }
}
```

- Delete `data/state.json` to force a full re-sync
- Logs are written to `data/immich-sync.log` and stdout

## Limitations

- **Cookie auth is fragile.** Amazon cookies expire and must be refreshed manually. There is no way around this - Amazon Photos has no OAuth or API key flow. When cookies expire, the container will log 401 errors until you update `.env` and restart.
- **Undocumented API.** This uses Amazon's internal Drive v1 endpoints. Amazon could change or remove them without notice.
- **Tested on a US account only.** Regional differences (content upload endpoints, cookie names, API behavior) may cause issues. The content upload URL is hardcoded to `content-na.drive.amazonaws.com` - other regions likely use different hostnames.
- **Uploads go to root folder.** Amazon Drive v1 API doesn't reliably support folder targeting for all account types. Photos land in your Amazon Photos root.
- **No content deduplication.** Dedup is by Immich asset ID, not file hash. Re-uploading the same photo with a different Immich ID will create a duplicate on Amazon.
- **Deletion moves to trash.** Removed photos are trashed, not permanently deleted. Empty your Amazon Photos trash manually if needed.

## License

MIT
