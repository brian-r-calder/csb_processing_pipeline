#!/bin/sh
CONTENT_ROOT=$(realpath "$(dirname $0)/..")

source ${CONTENT_ROOT}/scripts/init.sh

AWS_ACCESS_KEY_ID=$(grep 'Key ID:' ${CONTENT_ROOT}/${GARAGE_KEY_FILENAME} | cut -d ':' -f 2 | tr -d '[:blank:]\r')
AWS_SECRET_ACCESS_KEY=$(grep 'Secret key:' ${CONTENT_ROOT}/${GARAGE_KEY_FILENAME} | cut -d ':' -f 2 | tr -d '[:blank:]\r')
