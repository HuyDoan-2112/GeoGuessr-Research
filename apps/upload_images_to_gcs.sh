#!/usr/bin/env bash
#
# Upload /mnt/hans_disk/mapcrunch_images to a GCS bucket.
#
# Layout on disk:   /mnt/hans_disk/mapcrunch_images/{pano_id}/{heading}_{pitch}_{zoom}.jpg
# Layout on GCS:    gs://${BUCKET}/{pano_id}/{heading}_{pitch}_{zoom}.jpg
#
# `gcloud storage rsync -r` is resumable: rerun after an interruption and it
# only re-sends files that differ (size / md5). Pick a region that matches
# where the eval compute will run — same-region reads are free.
#
# Usage:
#   BUCKET=my-mapcrunch-bucket REGION=us-central1 ./apps/upload_images_to_gcs.sh
#
set -euo pipefail

SRC="${SRC:-/mnt/hans_disk/mapcrunch_images}"
BUCKET="${BUCKET:?set BUCKET=<gcs-bucket-name>}"
REGION="${REGION:-us-central1}"
STORAGE_CLASS="${STORAGE_CLASS:-STANDARD}"

if ! command -v gcloud >/dev/null; then
    echo "gcloud not found — install the Google Cloud SDK first" >&2
    exit 1
fi

if [[ ! -d "$SRC" ]]; then
    echo "source dir not found: $SRC" >&2
    exit 1
fi

if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
    echo "Creating bucket gs://${BUCKET} in ${REGION} (${STORAGE_CLASS})"
    gcloud storage buckets create "gs://${BUCKET}" \
        --location="${REGION}" \
        --default-storage-class="${STORAGE_CLASS}" \
        --uniform-bucket-level-access
fi

echo "Syncing ${SRC} -> gs://${BUCKET}/"
echo "(this will take a while for 7+TB; safe to Ctrl-C and rerun)"

gcloud storage rsync \
    --recursive \
    --continue-on-error \
    --exclude='\..*' \
    "${SRC}" "gs://${BUCKET}"

echo "Done. Verify with:  gcloud storage du -s gs://${BUCKET}"
