#!/usr/bin/env sh
DOCKER_COMPOSE="docker compose -f ${CONTENT_ROOT}/tests/docker-compose.yml"

AWS_REGION=garage
AWS_ENDPOINT_URL='http://127.0.0.1:3900'
AWS_CLI="aws --endpoint-url "${AWS_ENDPOINT_URL}" --region ${AWS_REGION}"
GARAGE_KEY_FILENAME=.garage-app-key
BUCKET_NAME='csb-dest'
