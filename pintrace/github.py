"""Read-only GitHub access with local caches and bounded retries."""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests


def github_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("GitHub authentication unavailable; use gh auth login")
    return result.stdout.strip()


class GitHub:
    def __init__(self, cache, token):
        self.cache = Path(cache)
        self.token = token
        self.session = requests.Session()
        self.cache.mkdir(parents=True, exist_ok=True)

    def request(self, url, authenticated=False):
        headers = {"User-Agent": "PinTrace research pilot"}
        if authenticated:
            if not url.startswith("https://api.github.com/"):
                raise ValueError("Credentials may only be sent to api.github.com")
            headers.update(Authorization="Bearer " + self.token,
                           Accept="application/vnd.github+json",
                           **{"X-GitHub-Api-Version": "2022-11-28"})
        for attempt in range(3):
            try:
                response = self.session.get(url, headers=headers, timeout=(15, 45))
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
                time.sleep(2 ** (attempt + 1))
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(min(30, int(response.headers.get("Retry-After", 2 ** (attempt + 1)))))
                continue
            response.raise_for_status()
            return response
        raise RuntimeError("Request retries exhausted")

    def api(self, endpoint, **params):
        url = "https://api.github.com/" + endpoint.lstrip("/")
        if params:
            url += "?" + urlencode(sorted(params.items()))
        path = self.cache / "api" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["data"]
        response = self.request(url, authenticated=True)
        data = response.json()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "retrieved_at": response.headers.get("Date"),
                                    "link": response.headers.get("Link"), "data": data}), encoding="utf-8")
        return data

    def blob(self, repository, commit, path, blob_sha):
        cached = self.cache / "blobs" / blob_sha
        if cached.exists():
            content = cached.read_bytes()
        else:
            url = f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(path, safe='/')}"
            content = self.request(url).content
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if actual != blob_sha:
            raise ValueError("Downloaded content does not match the Git tree blob")
        if not cached.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(content)
        return content

