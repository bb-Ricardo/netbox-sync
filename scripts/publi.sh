#!/usr/bin/env bash
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

BASE_PATH="$(realpath "$(dirname "${0}")/..")"
# shellcheck disable=SC2181
[[ ${?} -ne 0 ]] && exit 1

cd "${BASE_PATH}" || exit 1

IMAGE_NAME="bbricardo/netbox-sync"
IMAGE_PLATFORM="linux/arm/v7,linux/arm64/v8,linux/amd64"
IMAGE_PLATFORM="linux/amd64" # currently only amd64 due to too many dependencies when installing vmware python sdk
IMAGE_TAG=$(grep "^__version__" module/__init__.py | sed 's/__version__ = "\(.*\)"/\1/g')

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

find module -type d -name "__pycache__" -print0 | xargs -0 -n1 rm -rf
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
