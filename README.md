# GeoGuessr Research

Research toolkit for driving Google Street View with a remote database of panorama + configurations.

## Quickstart (locally hosted server)
1) Download requirements.txt into a conda env called bfcl_server.

2) Copy paste this into your .env:
```bash
# Path A / geoguessr_server_gcs.py configuration
# Loaded by python-dotenv when running the shim.

# Absolute path to the local SQLite metadata DB (downloaded from GCS once).
  MAPCRUNCH_DB_PATH=/absolute/path/to/GeoGuessr-Research/mapcrunch.db

# GCS bucket holding the rendered pano JPEGs.
MAPCRUNCH_GCS_BUCKET=geoguesr
MAPCRUNCH_GCS_PREFIX=mapcrunch_images/

# Local on-disk LRU cache for fetched JPEGs.
MAPCRUNCH_CACHE_DIR=/tmp/mapcrunch_cache
MAPCRUNCH_CACHE_SIZE_GB=5

# ADC requires *some* project for billing; any value works for read-only
# bucket access on a bucket you don't own.
GOOGLE_CLOUD_PROJECT=placeholder

# Flask server binding.
HOST=127.0.0.1
PORT=18000
```

## Command to start server:
command=conda run --no-capture-output -n bfcl_server python -m apps.geoguessr_server_gcs
