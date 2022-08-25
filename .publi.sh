#!/usr/bin/env bash

IMAGE_NAME="bbricardo/netbox-sync"
IMAGE_PLATFORM="linux/arm/v7,linux/arm64/v8,linux/amd64"
IMAGE_PLATFORM="linux/amd64" # currently only amd64 due to too many dependencies when installing vmware python sdk
IMAGE_TAG=$(grep "^__version__" netbox-sync.py | sed 's/__version__ = "\(.*\)"/\1/g')

if [[ -z "$IMAGE_TAG" ]]; then
  echo "ERROR: unable to grep version from 'netbox-sync.py'"
  exit 1
fi

read -rp "Is '$IMAGE_TAG' a beta (b) release or final (f) release: " -n1 ANSWER && echo

[[ $ANSWER =~ [bB] ]] && FINAL=false
[[ $ANSWER =~ [fF] ]] && FINAL=true
[[ -z "${FINAL+default}" ]] && echo "Please select 'b' or 'f'." && exit 1

unset DOCKER_TLS_VERIFY
unset DOCKER_HOST
unset DOCKER_CERT_PATH

find . -name "__pycache__" -delete
docker --config ./docker-tmp login
docker --config ./docker-tmp buildx create --use
if [[ $FINAL == true ]]; then
  docker --config ./docker-tmp buildx build --push \
    --platform ${IMAGE_PLATFORM} \
    --tag "${IMAGE_NAME}:latest" \
    --tag "${IMAGE_NAME}:${IMAGE_TAG}" .
  # shellcheck disable=SC2181
  [[ ${?} -ne 0 ]] && exit 1
  which docker-pushrm >/dev/null 2>&1 &&  docker-pushrm ${IMAGE_NAME}:latest
else
  docker --config ./docker-tmp buildx build --push \
    --platform ${IMAGE_PLATFORM} \
    --tag "${IMAGE_NAME}:${IMAGE_TAG}" .
fi

rm -rf ./docker-tmp

# EOF
