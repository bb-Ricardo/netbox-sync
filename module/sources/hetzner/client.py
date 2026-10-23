# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from hcloud import Client


class HetznerClient:

    def __init__(self, token):
        self.client = Client(token=token)

    def get_servers(self):
        return self.client.servers.get_all()
