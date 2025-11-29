#!/usr/bin/env python3
"""
Egress proxy for sandbox containers.

A simple HTTP/HTTPS proxy that validates outbound requests against an allowlist.
The allowlist is encoded in a JWT passed via proxy authentication.

Usage:
    python egress_proxy.py --port 15004 --signing-key <key>

Containers connect using:
    HTTP_PROXY=http://sandbox:jwt_<token>@<proxy_host>:15004
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Optional

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Default allowed hosts if no JWT is provided
DEFAULT_ALLOWED_HOSTS = [
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "crates.io",
    "static.crates.io",
]


class EgressProxy:
    def __init__(self, signing_key: str, default_allowed_hosts: list[str] | None = None):
        self.signing_key = signing_key
        self.default_allowed_hosts = default_allowed_hosts or DEFAULT_ALLOWED_HOSTS

    def _verify_jwt(self, token: str) -> Optional[dict]:
        """Verify and decode a JWT token."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None

            header_b64, payload_b64, signature_b64 = parts

            # Verify signature
            message = f"{header_b64}.{payload_b64}"
            expected_sig = hmac.new(
                self.signing_key.encode(),
                message.encode(),
                hashlib.sha256,
            ).digest()
            expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()

            if not hmac.compare_digest(signature_b64, expected_sig_b64):
                logger.warning("JWT signature verification failed")
                return None

            # Decode payload
            # Add padding if needed
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding

            payload = json.loads(base64.urlsafe_b64decode(payload_b64))

            # Check expiration
            if payload.get("exp", 0) < time.time():
                logger.warning("JWT expired")
                return None

            return payload

        except Exception as e:
            logger.warning(f"JWT verification error: {e}")
            return None

    def _extract_token_from_auth(self, auth_header: str) -> Optional[str]:
        """Extract JWT token from Proxy-Authorization header."""
        if not auth_header:
            return None

        # Format: Basic base64(username:password)
        # We expect: Basic base64(sandbox:jwt_<token>)
        if not auth_header.startswith("Basic "):
            return None

        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            if ":" not in decoded:
                return None

            username, password = decoded.split(":", 1)
            if not password.startswith("jwt_"):
                return None

            return password[4:]  # Remove "jwt_" prefix

        except Exception:
            return None

    def _get_allowed_hosts(self, request: web.Request) -> list[str]:
        """Get allowed hosts from JWT or use defaults."""
        auth_header = request.headers.get("Proxy-Authorization", "")
        token = self._extract_token_from_auth(auth_header)

        if token:
            payload = self._verify_jwt(token)
            if payload and "allowed_hosts" in payload:
                hosts = payload["allowed_hosts"]
                if isinstance(hosts, str):
                    return [h.strip() for h in hosts.split(",")]
                return hosts

        return self.default_allowed_hosts

    def _is_host_allowed(self, host: str, allowed_hosts: list[str]) -> bool:
        """Check if a host is in the allowlist (supports wildcards)."""
        # Remove port if present
        if ":" in host:
            host = host.split(":")[0]

        host = host.lower()

        for allowed in allowed_hosts:
            allowed = allowed.lower()

            # Exact match
            if host == allowed:
                return True

            # Wildcard match (*.example.com matches sub.example.com)
            if allowed.startswith("*."):
                suffix = allowed[1:]  # .example.com
                if host.endswith(suffix) or host == allowed[2:]:
                    return True

        return False

    async def handle_connect(self, request: web.Request) -> web.StreamResponse:
        """Handle HTTPS CONNECT requests."""
        # CONNECT requests have the host:port in the path
        target = request.path_qs
        if ":" in target:
            host, port = target.rsplit(":", 1)
            port = int(port)
        else:
            host = target
            port = 443

        allowed_hosts = self._get_allowed_hosts(request)

        if not self._is_host_allowed(host, allowed_hosts):
            logger.warning(f"Blocked CONNECT to {host}:{port}")
            return web.Response(
                status=403,
                text=f"Host not allowed: {host}",
            )

        logger.info(f"Proxying CONNECT to {host}:{port}")

        try:
            # Connect to target
            reader, writer = await asyncio.open_connection(host, port)

            # Send 200 Connection Established
            response = web.StreamResponse(
                status=200,
                reason="Connection Established",
            )
            response.force_close()
            await response.prepare(request)

            # Get the underlying transport
            transport = request.transport

            # Bidirectional pipe
            async def pipe(reader, writer):
                try:
                    while True:
                        data = await reader.read(8192)
                        if not data:
                            break
                        writer.write(data)
                        await writer.drain()
                except Exception:
                    pass
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass

            # Create tasks for bidirectional copying
            client_reader = request.content
            await asyncio.gather(
                pipe(client_reader, writer),
                pipe(reader, response),
                return_exceptions=True,
            )

            return response

        except Exception as e:
            logger.error(f"CONNECT error to {host}:{port}: {e}")
            return web.Response(
                status=502,
                text=f"Failed to connect: {e}",
            )

    async def handle_request(self, request: web.Request) -> web.Response:
        """Handle regular HTTP proxy requests."""
        # For CONNECT method, use special handler
        if request.method == "CONNECT":
            return await self.handle_connect(request)

        # Get the target URL from the request
        url = request.path_qs
        if not url.startswith("http://"):
            return web.Response(
                status=400,
                text="Invalid proxy request",
            )

        # Parse host from URL
        match = re.match(r"http://([^/:]+)", url)
        if not match:
            return web.Response(
                status=400,
                text="Could not parse host from URL",
            )

        host = match.group(1)
        allowed_hosts = self._get_allowed_hosts(request)

        if not self._is_host_allowed(host, allowed_hosts):
            logger.warning(f"Blocked request to {host}")
            return web.Response(
                status=403,
                text=f"Host not allowed: {host}",
            )

        logger.info(f"Proxying {request.method} to {url}")

        # Forward the request
        try:
            async with aiohttp.ClientSession() as session:
                # Copy headers, excluding proxy-specific ones
                headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in ("host", "proxy-authorization", "proxy-connection")
                }

                async with session.request(
                    request.method,
                    url,
                    headers=headers,
                    data=await request.read(),
                    allow_redirects=False,
                ) as resp:
                    # Forward response
                    response = web.Response(
                        status=resp.status,
                        headers={
                            k: v
                            for k, v in resp.headers.items()
                            if k.lower() not in ("transfer-encoding", "content-encoding")
                        },
                        body=await resp.read(),
                    )
                    return response

        except Exception as e:
            logger.error(f"Proxy error: {e}")
            return web.Response(
                status=502,
                text=f"Proxy error: {e}",
            )

    def create_app(self) -> web.Application:
        """Create the aiohttp application."""
        app = web.Application()
        # Catch all requests
        app.router.add_route("*", "/{path:.*}", self.handle_request)
        return app


def main():
    parser = argparse.ArgumentParser(description="Egress proxy for sandbox containers")
    parser.add_argument(
        "--port",
        type=int,
        default=15004,
        help="Port to listen on (default: 15004)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--signing-key",
        required=True,
        help="Key for JWT signature verification",
    )
    args = parser.parse_args()

    proxy = EgressProxy(signing_key=args.signing_key)
    app = proxy.create_app()

    logger.info(f"Egress proxy starting on {args.host}:{args.port}")
    logger.info(f"Default allowed hosts: {DEFAULT_ALLOWED_HOSTS}")

    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
