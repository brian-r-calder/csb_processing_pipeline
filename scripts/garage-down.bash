#!/usr/bin/env bash
set -eu -o pipefail
CONTENT_ROOT=$(realpath "$(dirname $0)/..")

source ${CONTENT_ROOT}/scripts/init.sh

${DOCKER_COMPOSE} down
rm -f ${CONTENT_ROOT}/${GARAGE_KEY_FILENAME}
