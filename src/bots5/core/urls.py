from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, urlsplit


_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def canonical_http_base_url(
    value: object,
    *,
    error_type: type[Exception] = ValueError,
    error_message: str = "base URL must be an HTTP/HTTPS API base URL",
) -> str:
    """Return the one URL spelling used for an OpenAI-compatible API base."""

    def fail() -> None:
        raise error_type(error_message)

    if type(value) is not str or not value or value != value.strip():
        fail()
    if "?" in value or "#" in value:
        fail()
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        fail()
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        fail()

    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            fail()
        canonical_host = f"[{hostname.lower()}]"
    else:
        try:
            canonical_hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            fail()
        trailing_dot = canonical_hostname.endswith(".")
        labels = (
            canonical_hostname[:-1].split(".")
            if trailing_dot
            else canonical_hostname.split(".")
        )
        if (
            not canonical_hostname
            or not labels
            or any(not label or not _HOST_LABEL.fullmatch(label) for label in labels)
            or len(canonical_hostname.rstrip(".")) > 253
        ):
            fail()
        if len(labels) == 4 and all(label.isdigit() for label in labels):
            try:
                ipaddress.IPv4Address(canonical_hostname)
            except ValueError:
                fail()
        canonical_host = canonical_hostname
    if port is not None and not 0 <= port <= 65535:
        fail()
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    canonical_port = "" if port is None or port == default_port else f":{port}"

    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
        fail()
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")

    def normalize_escape(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        return character if character in _UNRESERVED else match.group(0).upper()

    path = _PERCENT_ESCAPE.sub(normalize_escape, path)
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    canonical_path = "" if not segments else "/" + "/".join(segments)
    canonical = f"{parsed.scheme.lower()}://{canonical_host}{canonical_port}{canonical_path}"
    return canonical
