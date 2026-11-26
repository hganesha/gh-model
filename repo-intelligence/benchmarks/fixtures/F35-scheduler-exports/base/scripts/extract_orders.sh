#!/bin/sh
# Extract the day's orders into the staging area.
set -eu
mode="${1:-daily}"
out_dir="${BATCH_HOME:-/opt/batch}/stage/orders"
mkdir -p "$out_dir"
echo "extracting orders ($mode) into $out_dir"
