"""Operator-owned, site-independent HTTP proxy profiles."""
import re

from researchops.runners.research_fetch import validate_proxy_policy

PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_profiles(profiles):
    if not isinstance(profiles, dict) or len(profiles) > 32:
        raise ValueError("Invalid HTTP proxy profiles")
    result = {}
    for name, value in profiles.items():
        if (not isinstance(name, str) or not PROFILE_ID.fullmatch(name) or
                not isinstance(value, dict) or set(value) != {"proxy_url", "host_patterns"}):
            raise ValueError("Invalid HTTP proxy profile")
        validate_proxy_policy(value["proxy_url"], value["host_patterns"])
        if value["proxy_url"] is None:
            raise ValueError("HTTP proxy profile requires an endpoint")
        result[name] = {"proxy_url": value["proxy_url"], "host_patterns": list(value["host_patterns"])}
    return result


def resolve_http_profile(settings, runner):
    profiles = validate_profiles(settings.raw_config.get("http_proxy_profiles", {}))
    name = runner.get("http_proxy_profile")
    if name is None:
        return {}
    if not isinstance(name, str) or not PROFILE_ID.fullmatch(name) or name not in profiles:
        raise ValueError("Unknown HTTP proxy profile")
    if runner.get("network_profile") != "public-research" or runner.get("allow_http_requests") is not True:
        return {}
    return {"name": name, **profiles[name]}


def policy_options(profile):
    if not profile:
        return {}
    return {"proxy_url": profile["proxy_url"], "proxy_host_patterns": tuple(profile["host_patterns"])}
