# BFCL Street View server

This repository serves the HTTP API used by BFCL's
`bfcl_eval/eval_checker/multi_turn_eval/func_source_code/street_view.py`.
It reads panorama metadata from an existing SQLite database and pre-captured
JPEGs from GCS. The BFCL client itself stays in the BFCL repository.

The `geoguessr_type*_coords.csv` files and `locations.csv` are retained
research inputs. Keep these coordinate datasets when cleaning up the codebase,
even though the HTTP server does not read them directly.

## Run

Use Python 3.10 or newer:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `MAPCRUNCH_DB_PATH` to the downloaded metadata database and
`MAPCRUNCH_GCS_BUCKET` to the image bucket. Configure Application Default
Credentials with permission to read that bucket, and set
`GOOGLE_CLOUD_PROJECT` to your project. Then start the server:

```sh
python -m apps.geoguessr_server_gcs
```

The default address is `http://127.0.0.1:18000`, matching the BFCL client's
`GEOGUESSR_SERVER_URL` default. `.env` supplies defaults; exported environment
variables take precedence. `HOST`, `PORT`, and `SERVER_THREADS` control the
Waitress server. This service keeps sessions in one process; use threads for
concurrent BFCL clients.

`SERVER_THREADS` controls the number of requests processed at once.
`SERVER_CONNECTION_LIMIT` separately controls accepted HTTP connections, including
idle keep-alive connections (default `100`). Increasing `SERVER_THREADS` does not
increase that connection limit. Waitress normally closes idle HTTP connections
after 120 seconds; navigation sessions survive reconnects.

BFCL defaults locally hosted model runs containing GeoGuessr to 8 workers,
including mixed-category runs. API models default to 1. Independently of
`--num-threads`, updated BFCL runs on the same machine share an enforced limit of
8 active GeoGuessr episodes per server. Extra workers wait locally before
connecting, and the slot stays held until session teardown finishes. OS file
locks release the slot if a process exits, including when killed. Use the same
server URL across runs; loopback aliases share one budget. The lock files live
in `/tmp/bfcl-geoguessr-slots` on Unix, independently of result directories.
This leaves connection headroom below the default `SERVER_CONNECTION_LIMIT` of
100. Other clients and runners on different machines need separate coordination.
Raising the connection limit also requires enough available file descriptors.

SQLite connections are opened only for metadata reads and closed before those
operations return. Paused or abandoned sessions retain navigation state without
holding a database connection. Each `/connect` also reclaims sessions idle for
12 hours by default. `SESSION_IDLE_TIMEOUT` configures this timeout in seconds
(default `43200`). Requests in progress are protected; their idle timer restarts
when they finish, including when they fail. Cleanup runs on new connections,
without a background worker or client heartbeat.

For Docker, also set `GOOGLE_APPLICATION_CREDENTIALS` to the absolute path of
an ADC JSON credential file, then run `docker compose up --build`. Compose
mounts the database and credentials read-only and stores cached images in a
named volume.

## Supported contract

Every response has `ok`, `updates`, and `error` fields. `/connect` returns a
fresh `session_id`; all subsequent requests use the `X-Session-ID` header.
Google Maps credentials optionally sent by BFCL are accepted and ignored.

| Method | Route | Input / output |
| --- | --- | --- |
| POST | `/connect` | Returns `session_id` |
| POST | `/init_panorama` | `lat`, `lng`, optional `heading`, `pitch`, `zoom`; returns `available_moves` |
| GET | `/check/direction` | Returns compass `description` and `available_moves` |
| POST | `/move/<direction>` | Eight compass directions; returns `available_moves` |
| POST | `/scroll/<direction>` | `left`, `right`, `up`, `down` with `delta`; returns `available_moves` |
| POST | `/zoom/<direction>` | `in`, `out` with `delta`; returns `available_moves` |
| POST | `/capture/view` | Returns raw JPEG base64 in `image_base64` |
| POST | `/end_session` | Releases the session and clears its navigation state |

Initialization chooses the nearest stored panorama. The existing capture grid
is preserved: headings every 30 degrees, pitches from -30 to 30 in 15-degree
steps, and zoom levels 0, 1, 2. Initialization requires values on that grid.
Scroll and zoom convert `abs(delta)` to steps using Python `round`; heading
wraps and pitch/zoom clamp at the grid limits. Navigation fetches no images;
BFCL requests a capture separately after scrolling or zooming.

The database must contain `panoramas(pano_id, lat, lng, metadata_json)` and
`panorama_links(from_pano_id, to_pano_id, heading)`.
Links in `metadata_json` take precedence; the links table is the fallback.
GCS objects use the existing layout
`<prefix>/<pano_id with dots replaced by underscores>/<heading>_<pitch>_<zoom>.jpg`,
with float values such as `0.0_0.0_1.0.jpg`.

`MAPCRUNCH_CACHE_DIR` and `MAPCRUNCH_CACHE_SIZE_GB` configure the shared disk
cache; size 0 disables it. Cache entries are isolated by bucket and object.
The new cache lives in a `bfcl-images/` subdirectory; any legacy cache files
outside that directory can be deleted after migration.
`GEOGUESSR_CROP=off|top|bottom` controls the existing image ablation. Cropping
happens in memory; cached originals remain unchanged.

The former live Google Maps/Playwright backend, local-image fallback, crawlers,
upload tools, demos, CLI, and duplicate BFCL-style wrapper have been removed.

## Verify

```sh
pip install -r requirements-dev.txt
python -m pytest
ruff check apps tests
```

Tests use temporary SQLite databases and fake image storage, without cloud
credentials. The consumer compatibility test also exercises the real BFCL
client when it is available at the sibling `gorilla-hans` checkout, or at
`BFCL_STREET_VIEW_PATH`. That external-client test is skipped if the file is
absent; the self-contained server tests still run.
