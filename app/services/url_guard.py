"""URL Security & SSRF Protection Guard.

Validates public web addresses before Playwright or any internal agent navigates to them:
1. Strict scheme enforcement (http/https only).
2. Hostname resolution & IP blocklist (rejects loopback, private, link-local, reserved, CGNAT).
3. robots.txt policy compliance check.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger("rpa.url_guard")

# Carrier-Grade NAT (RFC 6598: 100.64.0.0/10)
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def validate_public_url(url: str) -> tuple[bool, str, Optional[str]]:
    """Validate that a URL uses http(s) and resolves only to public, non-private IP addresses.

    Returns:
        (is_valid, reason, resolved_ip)
    """
    if not url or not isinstance(url, str):
        return False, "URL không hợp lệ hoặc để trống.", None

    url = url.strip()
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Không thể phân tích URL: {exc}", None

    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"Chỉ chấp nhận giao thức http hoặc https (nhận: '{parsed.scheme}').", None

    hostname = parsed.hostname
    if not hostname:
        return False, "URL thiếu tên miền (hostname).", None

    hostname_lower = hostname.lower()
    if hostname_lower in ("localhost", "local", "broadcasthost") or hostname_lower.endswith((".local", ".internal", ".localhost")):
        return False, f"Tên miền '{hostname}' thuộc mạng cục bộ / nội bộ (bị chặn chống SSRF).", None

    # Determine port
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)

    # Resolve DNS
    try:
        addr_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"Không thể phân giải DNS cho tên miền '{hostname}': {exc}", None
    except Exception as exc:
        return False, f"Lỗi DNS khi tra cứu '{hostname}': {exc}", None

    if not addr_info:
        return False, f"Không tìm thấy bản ghi DNS cho '{hostname}'.", None

    resolved_ips = list({info[4][0] for info in addr_info if info[4]})

    for raw_ip in resolved_ips:
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False, f"Địa chỉ IP không hợp lệ: {raw_ip}", raw_ip

        if ip_obj.is_loopback:
            return False, f"Địa chỉ IP '{raw_ip}' là loopback (127.0.0.1/::1), bị chặn chống SSRF.", raw_ip
        if ip_obj.is_private:
            return False, f"Địa chỉ IP '{raw_ip}' thuộc dải mạng nội bộ (private IP), bị chặn chống SSRF.", raw_ip
        if ip_obj.is_link_local:
            return False, f"Địa chỉ IP '{raw_ip}' thuộc link-local (169.254.x.x), bị chặn chống SSRF.", raw_ip
        if ip_obj.is_reserved:
            return False, f"Địa chỉ IP '{raw_ip}' thuộc dải IETF reserved, bị chặn.", raw_ip
        if ip_obj.is_multicast:
            return False, f"Địa chỉ IP '{raw_ip}' là multicast, bị chặn.", raw_ip
        if ip_obj.is_unspecified:
            return False, f"Địa chỉ IP '{raw_ip}' là unspecified (0.0.0.0), bị chặn.", raw_ip
        if ip_obj.version == 4 and ip_obj in _CGNAT_NET:
            return False, f"Địa chỉ IP '{raw_ip}' thuộc mạng chia sẻ CGNAT, bị chặn.", raw_ip

    return True, "URL công khai an toàn.", resolved_ips[0] if resolved_ips else None


async def check_robots_txt(url: str, user_agent: str = "*") -> tuple[bool, str]:
    """Check if the given URL is allowed by the target website's robots.txt.

    Returns:
        (is_allowed, note)
    """
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            resp = await client.get(robots_url, headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
            if resp.status_code == 200:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
                allowed = rp.can_fetch(user_agent, url)
                if not allowed:
                    return False, f"Đường dẫn bị cấm cào theo chính sách robots.txt của {parsed.netloc}."
                return True, "Cho phép bởi robots.txt."
            elif resp.status_code in (401, 403):
                # Forbidden robots.txt -> conservative: disallow
                return False, f"Website cấm truy cập robots.txt (HTTP {resp.status_code})."
            else:
                # 404 or other: robots.txt doesn't exist -> permissible
                return True, "Không có hạn chế trong robots.txt."
    except Exception as exc:
        log.debug("robots.txt check skipped/failed for %s: %s", url, exc)
        return True, "Không thể kiểm tra robots.txt (bỏ qua)."
