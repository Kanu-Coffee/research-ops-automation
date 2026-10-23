"""Credential-free Research client staged with a fixed invocation profile.

This convenience client is not a boundary against hostile shell code. It uses
the same bounded, DNS-pinned transport as application-owned media acquisition.
"""
import json
from pathlib import Path

from researchops.runners.research_fetch import FetchPolicy, PublicResearchFetcher


def fetch_public(url, *, method="GET", json_body=None, headers=None,
                 timeout_seconds=20, max_bytes=1_048_576):
    profile = json.loads(Path(__file__).with_name("http-profile.json").read_bytes())
    policy = FetchPolicy(timeout_seconds=timeout_seconds, max_body_bytes=max_bytes,
        require_https=True, allow_json_post=True,
        proxy_url=profile.get("proxy_url"), proxy_host_patterns=tuple(profile.get("host_patterns", [])))
    return PublicResearchFetcher(policy).fetch(url, method=method, json_body=json_body, headers=headers)
