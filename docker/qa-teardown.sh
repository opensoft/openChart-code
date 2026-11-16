#!/usr/bin/env bash
# Destroy the QA stack AND its data. For an ordinary stop use `qa.sh down`.

set -euo pipefail

# The project name, the compose wrapper (always -p open-chart-qa) and the
# foreign-stack guard are qa.sh's. Sourcing it runs nothing but its definitions.
# shellcheck source=docker/qa.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/qa.sh"

require_docker
# `down --volumes` is the destructive one: refuse a stack created from another
# checkout unless QA_ALLOW_FOREIGN_STACK=1.
require_own_stack

echo "== OC-QA-STEP: stop stack and wipe persistent QA volumes"
compose down --volumes --remove-orphans
echo "OC-QA-RESULT: WIPED (database, bench home, sites, and credentials removed)"
