#!/usr/bin/env bash

IMAGE_NAME="bbricardo/netbox-sync"
IMAGE_TAG=$(grep "^__version__" netbox-sync.py | sed 's/__version__ = "\(.*\)"/\1/g')

if [[ -z "$IMAGE_TAG" ]]; then
  echo "ERROR: unable to grep version from 'netbox-sync.py'"
  exit 1
fi

find . -name "__pycache__" -delete
docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" .
docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "${IMAGE_NAME}:latest"
docker push "${IMAGE_NAME}:${IMAGE_TAG}"
docker push "${IMAGE_NAME}:latest"

docker-pushrm "$IMAGE_NAME"
