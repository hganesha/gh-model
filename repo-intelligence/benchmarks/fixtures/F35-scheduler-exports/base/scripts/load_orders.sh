#!/bin/sh
# Load the staged orders for a business date.
set -eu
business_date="${1:?business date}"
echo "loading orders for $business_date"
