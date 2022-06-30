#!/usr/bin/env bash

[[ -z "$1" ]] && echo "please define a version tag" && exit

IMAGE_NAME="bbricardo/netbox-sync"

docker --config ./docker-tmp login
docker --config ./docker-tmp buildx create --use
docker --config ./docker-tmp buildx build --push \
  --platform linux/arm/v7,linux/arm64/v8,linux/amd64 \
  --tag ${IMAGE_NAME}:latest \
  --tag ${IMAGE_NAME}:${1} .

rm rf ./docker-tmp

# EOF