#!/usr/bin/env bash
set -eu -o pipefail
CONTENT_ROOT=$(realpath "$(dirname $0)/..")

source ${CONTENT_ROOT}/scripts/init.sh

# Bring up Garage docker compose stack
${DOCKER_COMPOSE} up --wait

echo "Getting garage node IDs (${DOCKER_COMPOSE})..."
garage_nodes=($(${DOCKER_COMPOSE} exec -t garage /garage status | grep 'NO ROLE ASSIGNED' | cut -d ' ' -f 1 | tr -d '[:blank:]'))
num_garage_nodes=${#garage_nodes[@]}
echo "Found ${num_garage_nodes} node(s)."
# Set cluster layout for remaining nodes
for (( i=0; i<${num_garage_nodes}; i++ ));
do
  echo "Setting up storage for node ${garage_nodes[$i]}..."
  ${DOCKER_COMPOSE} exec -t garage /garage layout assign -z dc1 -c 100G "${garage_nodes[$i]}"
done
echo "Applying garage cluster layout changes..."
${DOCKER_COMPOSE} exec -t garage /garage layout apply --version 1

# Create access key and allow it to create buckets
echo "Creating access key"
${DOCKER_COMPOSE} --ansi never exec -t garage /garage > ${CONTENT_ROOT}/${GARAGE_KEY_FILENAME} key create garage-app-key
${DOCKER_COMPOSE} exec -t garage /garage key allow --create-bucket garage-app-key

# Get AWS credentials
export AWS_ACCESS_KEY_ID=$(grep 'Key ID:' ${CONTENT_ROOT}/${GARAGE_KEY_FILENAME} | cut -d ':' -f 2 | tr -d '[:blank:]\r')
export AWS_SECRET_ACCESS_KEY=$(grep 'Secret key:' ${CONTENT_ROOT}/${GARAGE_KEY_FILENAME} | cut -d ':' -f 2 | tr -d '[:blank:]\r')

echo "Creating bucket ${BUCKET_NAME}..."
$AWS_CLI s3api create-bucket --create-bucket-configuration LocationConstraint=$AWS_REGION \
      --bucket $BUCKET_NAME

echo "Listing buckets..."
$AWS_CLI s3api list-buckets
