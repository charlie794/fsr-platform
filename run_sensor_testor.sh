#!/usr/bin/env bash
# Launches Sensor Testor as a proper package (python -m Sensor_Testor.main).
# This script lives INSIDE the Sensor_Testor folder, so its parent directory
# is the one that must be on the path for the package import to resolve.

# Directory this script is in = .../Sensor_Testor
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Parent = the folder that CONTAINS Sensor_Testor
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PARENT_DIR" || exit 1
exec python3 -m Sensor_Testor.main