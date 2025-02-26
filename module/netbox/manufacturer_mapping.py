# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import re

AMD = "AMD"
BROADCOM = "Broadcom"
CISCO = "Cisco"
DELL = "Dell"
FUJITSU = "Fujitsu"
HISILICON = "HiSilicon"
HPE = "HPE"
HUAWEI = "Huawei"
HYNIX = "Hynix"
INSPUR = "Inspur"
INTEL = "Intel"
LENOVO = "Lenovo"
MICRON = "Micron"
NVIDIA = "Nvidia"
SAMSUNG = "Samsung"
SUPERMICRO = "Supermicro"
TOSHIBA = "Toshiba"
WD = "Western Digital"

# Key must be a regex expression
manufacturer_mappings = {
    "^AMD$": AMD,
    ".*Broadcom.*": BROADCOM,
    ".*Cisco.*": CISCO,
    ".*Dell.*": DELL,
    "FTS Corp": FUJITSU,
    ".*Fujitsu.*": FUJITSU,
    ".*HiSilicon.*": HISILICON,
    "^HP$": HPE,
    "^HPE$": HPE,
    ".*Huawei.*": HUAWEI,
    ".*Hynix.*": HYNIX,
    ".*Inspur.*": INSPUR,
    ".*Intel.*": INTEL,
    "LEN": LENOVO,
    ".*Lenovo.*": LENOVO,
    ".*Micron.*": MICRON,
    ".*Nvidea.*": NVIDIA,
    ".*Samsung.*": SAMSUNG,
    ".*Supermicro.*": SUPERMICRO,
    ".*Toshiba.*": TOSHIBA,
    "^WD$": WD,
    ".*Western Digital.*": WD
}

compiled_manufacturer_mappings = dict()

for regex_expression, name in manufacturer_mappings.items():
    try:
        compiled_manufacturer_mappings[re.compile(regex_expression, flags=re.IGNORECASE)] = name
    except re.error:
        raise ValueError(f"Unable to compile regular expression '{regex_expression}'")


def sanitize_manufacturer_name(manufacturer_name):

    if manufacturer_name is None:
        return

    for regex_test, resolved_name in compiled_manufacturer_mappings.items():
        if regex_test.match(manufacturer_name):
            return resolved_name

    return manufacturer_name

# EOF
