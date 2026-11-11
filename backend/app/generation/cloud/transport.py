"""Deadline-bound HTTPS and credential-free, DNS-pinned artifact downloads."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx
from app.generation.models import GenerationError
from app.generation.failures import PUBLIC_FAILURES, TRANSIENT_QUERY_FAILURES

REGIONS = r"(?:cn-beijing|cn-hongkong|ap-southeast-1|us-east-1|eu-central-1|ap-northeast-1)"
DEDICATED = re.compile(r"[a-zA-Z0-9-]+\." + REGIONS + r"\.maas\.aliyuncs\.com$")
PUBLIC_HOSTS = {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
MAX_API_RESPONSE = 2 * 1024 * 1024
DNS_PROGRAM = ("import json,socket,sys; print(json.dumps(list(dict.fromkeys(row[4][0] for row in "
    "socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)))))")


def api_base(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v1"
            or not (parsed.hostname in PUBLIC_HOSTS or DEDICATED.fullmatch(parsed.hostname))):
        raise ValueError("生成 API 地址必须是百炼官方 HTTPS /api/v1 地址")
    return value.rstrip("/")


def failure(code, status=502):
    code = code if code in PUBLIC_FAILURES else "provider_failed"
    return GenerationError(status, PUBLIC_FAILURES[code], code)


def error_code(payload):
    """Classify documented codes, never infer a cause from arbitrary messages.

    Reference: https://help.aliyun.com/en/model-studio/error-code (2026-11-11).
    In particular, bare ResourceExhausted and Throttling.AllocationQuota mean
    request/token limits, while Throttling.ResourceExhausted means saturation.
    """
    value = payload.get("code", "") if isinstance(payload, dict) else ""
    if not isinstance(value, str) or len(value) > 160:
        return "provider_failed"
    code = value.strip().lower().replace("_", "").replace("-", "")
    if code in {"throttling.serviceoverloaded", "throttling.resourceexhausted",
                "insufficientresources", "insufficientcapacity", "capacityunavailable"}:
        return "provider_capacity_exhausted"
    if code in {"arrearage", "insufficientbalance", "prepaidbilloverdue", "postpaidbilloverdue", "isv.outofservice"}:
        return "provider_balance_insufficient"
    if code in {"allocationquota.freetieronly", "budgetlimitexceeded", "quotaexceeded", "servicequotaexhausted"}:
        return "provider_quota_exceeded"
    if code in {"datainspectionfailed", "inputdatainspectionfailed", "outputdatainspectionfailed",
                "invalidparameter.datainspection", "contentfilter", "contentrejected", "sensitivecontent"}:
        return "content_rejected"
    if code == "throttling" or code.startswith("throttling.") or code in {
            "ratelimited", "ratelimitexceeded", "limitrequests", "resourceexhausted", "insufficientquota", "toomanyrequests"}:
        return "rate_limited"
    if code in {"invalidapikey", "invalidaccesskeyid", "unauthorized", "authenticationfailed"}:
        return "provider_authentication_failed"
    if code in {"accessdenied", "endpoint.accessdenied", "commoditynotpurchased", "forbidden"}:
        return "provider_access_denied"
    if code in {"invalidparameter", "invalidparametererror", "invalidrequest", "invalidrequesterror", "modelnotfound"} or code.startswith(("invalidparameter.", "invalidfile.", "internalerror.algo.invalidparameter")):
        return "invalid_generation_inputs"
    if code in {"requesttimeout", "gatewaytimeout", "gatewaytimeout.inputdownload", "processingtimeout"}:
        return "provider_timeout"
    if code in {"serviceunavailable", "modelunavailable", "internalerror", "internalerror.algo"}:
        return "provider_unavailable"
    return "provider_failed"


class PublicResolver:
    """Resolve in a killed-and-reaped subprocess, with no parent credentials.

    A canceled asyncio resolver thread can delay asyncio.run shutdown. This tiny
    process is bounded independently, receives only the hostname, and never sees
    the parent's API keys, proxy credentials or request.
    """
    def __init__(self, resolver=None):
        self.resolver = resolver
        self.cache = {}
        self.lock = threading.Lock()

    def resolve(self, host, timeout):
        if timeout <= 0:
            raise failure("provider_timeout", 504)
        with self.lock:
            found = self.cache.get(host)
            if found and found[0] > time.monotonic():
                return found[1]
        try:
            if self.resolver is not None:  # Local mock DNS injection.
                ips = list(dict.fromkeys(row[4][0] for row in self.resolver(host, 443, type=socket.SOCK_STREAM)))
            else:
                result = subprocess.run([sys.executable, "-I", "-c", DNS_PROGRAM, host],
                    timeout=min(8.0, timeout), check=True, capture_output=True, text=True,
                    env={"PATH": os.defpath, "LANG": "C"})
                ips = json.loads(result.stdout)
            if not isinstance(ips, list) or not ips or any(not isinstance(ip, str) or not ipaddress.ip_address(ip).is_global for ip in ips):
                raise ValueError()
        except subprocess.TimeoutExpired:
            raise failure("provider_timeout", 504) from None
        except (OSError, subprocess.SubprocessError):
            raise failure("provider_network_error", 503) from None
        except ValueError:
            raise failure("unsafe_asset_url") from None
        with self.lock:
            self.cache[host] = (time.monotonic() + 60.0, ips[0])
        return ips[0]


class ApiTransport:
    def __init__(self, timeout, *, trust_env=False, transport=None, resolver=None):
        self.timeout = float(timeout)
        self.trust_env = trust_env
        self.transport = transport
        self.resolver = PublicResolver(resolver)

    def call(self, method, url, key, *, payload=None, asynchronous=False, allow_request_id_string=False):
        deadline = time.monotonic() + self.timeout
        parsed = httpx.URL(url)
        ip = self.resolver.resolve(parsed.host, deadline - time.monotonic())
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json", "Host": parsed.host}
        if asynchronous:
            headers["X-DashScope-Async"] = "enable"

        async def request():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(timeout=httpx.Timeout(remaining, connect=min(remaining, 8)),
                        limits=httpx.Limits(max_connections=1), transport=self.transport,
                        follow_redirects=False, trust_env=self.trust_env) as client:
                    async with client.stream(method, parsed.copy_with(host=ip), headers=headers, json=payload,
                            extensions={"sni_hostname": parsed.host}) as response:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_API_RESPONSE:
                                raise failure("provider_response_invalid")
                        try:
                            result = json.loads(data)
                        except (ValueError, UnicodeError):
                            result = None
                        if not 200 <= response.status_code < 300:
                            # Parse a structured code before the status fallback:
                            # a 403 Arrearage is billing, not an invalid API key.
                            code = error_code(result)
                            if code == "provider_failed":
                                code = ({401: "provider_authentication_failed", 403: "provider_access_denied",
                                         429: "rate_limited", 408: "provider_timeout", 504: "provider_timeout",
                                         400: "invalid_generation_inputs", 422: "invalid_generation_inputs"}
                                        .get(response.status_code, "provider_unavailable" if response.status_code >= 500 else "provider_failed"))
                            exc = failure(code, response.status_code)
                            if response.status_code == 429 and code in TRANSIENT_QUERY_FAILURES:
                                try:
                                    exc.retry_after = max(1.0, min(300.0, float(response.headers.get("retry-after", "15"))))
                                except ValueError:
                                    exc.retry_after = 15.0
                            raise exc
                        if allow_request_id_string and 200 <= response.status_code < 300 and isinstance(result, str) and result:
                            return {"request_id": result}
                        if not isinstance(result, dict):
                            raise failure("provider_response_invalid")
                        if result.get("code") and not isinstance(result.get("output"), dict):
                            raise failure(error_code(result))
                        return result
        try:
            return asyncio.run(request())
        except (TimeoutError, httpx.TimeoutException):
            raise failure("provider_timeout", 504) from None
        except httpx.HTTPError:
            raise failure("provider_network_error", 503) from None

    def close(self):
        # Each bounded call owns and closes its async client/socket.
        pass


class ArtifactDownloader:
    """Only fetch public Alibaba artifact hosts; pin IP and preserve TLS SNI."""
    def __init__(self, root, timeout, *, transport=None, resolver=None):
        self.root = Path(root)
        self.timeout = min(300.0, max(1.0, float(timeout)))
        self.transport = transport
        self.resolver = PublicResolver(resolver)

    def _pinned(self, value, timeout):
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            if (parsed.scheme != "https" or not host or not host.endswith(".aliyuncs.com")
                    or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or parsed.fragment):
                raise ValueError()
            ip = self.resolver.resolve(host, timeout)
            return httpx.URL(value).copy_with(host=ip), host
        except (ValueError, OSError):
            raise failure("unsafe_asset_url") from None

    def fetch(self, value, kind):
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        temporary = None
        maximum = (20 if kind == "image" else 256) * 1024 * 1024
        initial, initial_host = self._pinned(value, deadline - time.monotonic())

        async def download():
            nonlocal value, temporary
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            async with asyncio.timeout(remaining):
                for attempt in range(4):
                    if attempt == 0:
                        pinned, hostname = initial, initial_host
                    else:
                        pinned, hostname = self._pinned(value, deadline - time.monotonic())
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    # A fresh client verifies each hop's original TLS hostname.
                    async with httpx.AsyncClient(timeout=httpx.Timeout(remaining, connect=min(8, remaining)),
                            transport=self.transport, trust_env=False, follow_redirects=False) as client:
                        async with client.stream("GET", pinned, headers={"Host": hostname, "Accept-Encoding": "identity"},
                                extensions={"sni_hostname": hostname}) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                location = response.headers.get("location")
                                if not location:
                                    raise failure("unsafe_asset_url")
                                value = urljoin(value, location)
                                continue
                            if response.status_code != 200:
                                raise failure("provider_unavailable", 503)
                            if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                                raise failure("provider_response_invalid")
                            try:
                                length = int(response.headers.get("content-length", "0"))
                            except ValueError:
                                raise failure("provider_response_invalid") from None
                            if length < 0 or length > maximum:
                                raise GenerationError(413, "生成产物超过本机资产容量限制。", "asset_size_limit")
                            fd, name = tempfile.mkstemp(prefix=".cloud-", suffix=".png" if kind == "image" else ".mp4", dir=self.root)
                            temporary = Path(name)
                            size = 0
                            with os.fdopen(fd, "wb") as output:
                                async for chunk in response.aiter_bytes():
                                    size += len(chunk)
                                    if size > maximum:
                                        raise GenerationError(413, "生成产物超过本机资产容量限制。", "asset_size_limit")
                                    output.write(chunk)
                            if size <= 0:
                                raise failure("provider_response_invalid")
                            return temporary
                raise failure("unsafe_asset_url")
        try:
            return asyncio.run(download())
        except (TimeoutError, httpx.TimeoutException):
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise failure("provider_timeout", 504) from None
        except httpx.HTTPError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise failure("provider_network_error", 503) from None
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
