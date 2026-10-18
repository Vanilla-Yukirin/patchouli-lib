from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit


def validate_origin(value: str, *, allow_private_http: bool = False) -> str:
    """保持 HTTPS 默认值；HTTP 仅限调用者明确选择的私有 IP 入口。"""
    message = "base URL must be an HTTPS origin or an explicitly enabled private HTTP IP origin"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(message) from None
    if (
        any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or "?" in value
        or "#" in value
        or "\\" in value
        or port == 0
    ):
        raise ValueError(message)
    if parsed.scheme == "http":
        if allow_private_http is not True:
            raise ValueError(message)
        try:
            address = ip_address(parsed.hostname)
        except ValueError:
            raise ValueError(message) from None
        networks = (
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "::1/128",
            "fc00::/7",
        )
        if "%" in str(address) or not any(address in ip_network(net) for net in networks):
            raise ValueError(message)
    return value.rstrip("/")
