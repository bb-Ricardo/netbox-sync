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
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                detail = str(exc)
                try:
                    error_payload = response.json()
                except requests.exceptions.JSONDecodeError:
                    detail = f"{detail} (error response is not JSON)"
                else:
                    if isinstance(error_payload, dict):
                        detail = error_payload.get("message") or detail
                raise requests.HTTPError(
                    f"DigitalOcean API '{path}' returned HTTP {response.status_code}: {detail}",
                    response=response,
                ) from exc
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"DigitalOcean response for '{path}' is not a JSON object.")
            collection = payload.get(collection_name)
            # DigitalOcean can return {"databases": null} for accounts without databases.
            if collection_name == "databases" and "databases" in payload and collection is None:
                collection = []
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
