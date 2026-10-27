# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from urllib.parse import urljoin, urlparse

import requests


class DigitalOceanClient:

    api_url = "https://api.digitalocean.com/v2/"
    timeout = 30

    def __init__(self, token, session=None):
        self.session = session or requests.Session()
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "netbox-sync",
        }

    def get_collection(self, path, collection_name):
        url = urljoin(self.api_url, path.lstrip("/"))
        params = {"per_page": 200}
        objects = []

        while url is not None:
            response = self.session.get(
                url,
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"DigitalOcean response for '{path}' is not a JSON object.")
            collection = payload.get(collection_name)
            if not isinstance(collection, list):
                raise ValueError(f"DigitalOcean response for '{path}' has no '{collection_name}' list.")
            objects.extend(collection)

            links = payload.get("links") or {}
            if not isinstance(links, dict):
                raise ValueError(f"DigitalOcean response for '{path}' has invalid pagination links.")
            pages = links.get("pages") or {}
            if not isinstance(pages, dict):
                raise ValueError(f"DigitalOcean response for '{path}' has invalid pagination pages.")
            next_url = pages.get("next")
            if next_url:
                if not isinstance(next_url, str):
                    raise ValueError(f"DigitalOcean response for '{path}' has a non-string pagination link.")
                url = urljoin(url, next_url)
                parsed_url = urlparse(url)
                if parsed_url.scheme != "https" or parsed_url.hostname != "api.digitalocean.com" \
                        or parsed_url.port not in (None, 443) or parsed_url.username is not None:
                    raise ValueError("DigitalOcean API returned a pagination link outside its HTTPS API host.")
            else:
                url = None
            params = None

        return objects

    def close(self):
        self.session.close()
