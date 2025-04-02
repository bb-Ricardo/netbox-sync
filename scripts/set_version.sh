#!/usr/bin/env bash
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.


EXAMPLE_CONFIG_FILE="settings-example.ini"
VERSION_DATA_FILE="module/__init__.py"
README_FILE="README.md"
VERSION_TO_SET="$1"
COPYRIGHT_PATTERN="#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved."

BASE_PATH="$(realpath "$(dirname "${0}")/..")"
# shellcheck disable=SC2181
[[ ${?} -ne 0 ]] && exit 1

cd "${BASE_PATH}" || exit 1

[[ -z "${VERSION_TO_SET}" ]] && echo "no version defined. $0 \$version" && exit 1

read -rp "Should '$VERSION_TO_SET' be set as the new version [yN]: " -n1 ANSWER && echo
[[ ! "${ANSWER}" =~ [yY] ]] && exit 0

# setting new version and date
sed -i "" -e 's/^__version__.*/__version__ = "'"${VERSION_TO_SET}"'"/g' "${VERSION_DATA_FILE}"
sed -i "" -e 's/^__version_date__.*/__version_date__ = "'"$(date +%F)"'"/g' "${VERSION_DATA_FILE}"

# update config
[[ -e "${EXAMPLE_CONFIG_FILE}" ]] && rm "$EXAMPLE_CONFIG_FILE"
./netbox-sync.py -g -c "${EXAMPLE_CONFIG_FILE}"

# update help in README.md
README_TOP=$(sed '/# Running the script/q' "${README_FILE}")
README_BOTTOM=$(sed -n '/## TESTING/,$ p' "${README_FILE}")

{
  echo "${README_TOP}"
  echo -e "\n"'```'
  ./netbox-sync.py -h
  echo -e '```'"\n"
  echo "${README_BOTTOM}"
} > "${README_FILE}"

# update COPYRIGHT notice date
NEW_COPYRIGHT_NOTICE="${COPYRIGHT_PATTERN//..../$(date +%Y)}"
grep -lR "$COPYRIGHT_PATTERN" "${BASE_PATH}" | while read -r FILE; do
  sed -i "" -e 's/'"${COPYRIGHT_PATTERN}"'/'"${NEW_COPYRIGHT_NOTICE}"'/g' "${FILE}"
done

# EOF
