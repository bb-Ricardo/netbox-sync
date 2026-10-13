# -*- coding: utf-8 -*-
# Licensed under the MIT license.
import time
from urllib.parse import urljoin
import requests

class VMmanagerAPIError(RuntimeError):
    """VMmanager cannot provide a trustworthy inventory snapshot."""

class VMmanagerClient:
    """Read-only client for the documented VMmanager 6 API."""
    def __init__(self, base_url, email, password, verify_ssl=True, timeout=30, session=None):
        self.base_url = base_url.rstrip("/") + "/"
        self.email, self.password = email, password
        self.verify_ssl, self.timeout = verify_ssl, max(1, int(timeout))
        self.session = session or requests.Session()
        self._token = None

    def _url(self, path):
        return urljoin(self.base_url, path.lstrip("/"))

    def _request(self, method, path, *, json=None, authenticated=True):
        headers = {"x-xsrf-token": self._token} if authenticated and self._token else {}
        response = None
        for attempt in range(4):
            try:
                response = self.session.request(method, self._url(path), json=json, headers=headers,
                                                timeout=self.timeout, verify=self.verify_ssl)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # DNS failures, refused connections and timeouts can be transient.
                # Retry a few times, then report a controlled source error.
                if attempt == 3:
                    raise VMmanagerAPIError(
                        f"VMmanager is unreachable for {path} after 4 attempts: {exc}"
                    ) from exc
                time.sleep(0.5 * (attempt + 1))
                continue
            except requests.RequestException as exc:
                raise VMmanagerAPIError(f"VMmanager request failed for {path}: {exc}") from exc
            if response.status_code != 503 or attempt == 3:
                break
            time.sleep(0.25 * (attempt + 1))
        if response.status_code >= 400:
            message = f"HTTP {response.status_code}"
            try:
                body = response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                if error:
                    message += f": {error.get('msg', error)}"
            except (ValueError, AttributeError):
                pass
            raise VMmanagerAPIError(f"VMmanager API {path} failed: {message}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise VMmanagerAPIError(f"VMmanager API {path} returned invalid JSON") from exc
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            raise VMmanagerAPIError(f"VMmanager API {path} returned error: {error.get('msg', error) if isinstance(error, dict) else error}")
        return payload

    def authenticate(self):
        payload = self._request("POST", "auth/v4/public/token",
                                json={"email": self.email, "password": self.password}, authenticated=False)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise VMmanagerAPIError("VMmanager authentication response did not contain a token")
        self._token = token
        return token

    @staticmethod
    def _extract_list(payload):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise VMmanagerAPIError("VMmanager host list response is not a JSON object/list")
        for key in ("doc", "items", "list", "data", "hosts", "host"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for nested_key in ("item", "items", "host", "list"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return nested
        raise VMmanagerAPIError("Unrecognized VMmanager host list response; refusing to treat it as an empty inventory")

    def _get_paged_items(self, endpoint, item_label, page_size=50):
        """Return a complete, validated VMmanager list snapshot."""
        if not self._token:
            self.authenticate()
        page_size = max(1, int(page_size))
        offset, items, expected_total = 0, [], None
        while True:
            payload = self._request("GET", f"{endpoint}?limit={offset},{page_size}")
            batch = self._extract_list(payload)
            if any(not isinstance(item, dict) for item in batch):
                raise VMmanagerAPIError(f"VMmanager {item_label} list contains a non-object item")
            if any(item.get("id") is None or not str(item.get("id")).strip() for item in batch):
                raise VMmanagerAPIError(f"VMmanager returned a {item_label} without an ID; refusing to sync")
            if isinstance(payload, dict):
                size = payload.get("size")
                if isinstance(size, int) and size >= 0:
                    expected_total = size
            items.extend(batch)
            offset += len(batch)
            if expected_total is not None:
                if len(items) >= expected_total:
                    if len(items) != expected_total:
                        raise VMmanagerAPIError(f"VMmanager {item_label} snapshot exceeded reported total")
                    break
            elif len(batch) < page_size:
                break
            if not batch:
                break
        ids = [str(item["id"]) for item in items]
        if len(ids) != len(set(ids)):
            raise VMmanagerAPIError(f"VMmanager returned duplicate {item_label} IDs across pages")
        if expected_total is not None and len(items) != expected_total:
            raise VMmanagerAPIError(f"VMmanager {item_label} snapshot incomplete: expected {expected_total}, received {len(items)}")
        return items

    def get_hosts(self, page_size=50):
        """Return a complete VM snapshot using `/vm/v3/host?limit=offset,count`."""
        return self._get_paged_items("vm/v3/host", "VM", page_size)

    def get_disks(self, page_size=50):
        """Return a complete disk snapshot with per-disk `size_mib` values."""
        return self._get_paged_items("vm/v3/disk", "disk", page_size)

    def get_clusters(self, page_size=50):
        """Return a complete cluster snapshot using the VMmanager v3 API."""
        return self._get_paged_items("vm/v3/cluster", "cluster", page_size)

    def get_nodes(self, page_size=50):
        """Return a complete hypervisor-node snapshot using the VMmanager v3 API."""
        return self._get_paged_items("vm/v3/node", "node", page_size)
