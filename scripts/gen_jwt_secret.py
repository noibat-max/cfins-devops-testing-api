#!/usr/bin/env python3
"""Generate a JWT signing secret for the local API.

Prints a cryptographically-random URL-safe key (48 bytes = 384 bits, well above
the 256-bit minimum for HS256) as a ready-to-paste `JWT_SIGN_HASH=...` line.
Copy it into `.env` yourself. Local-only tooling; not part of the image.

Usage:
    python scripts/gen_jwt_secret.py

Env:
    JWT_SIGN_HASH_BYTES   entropy in bytes (default: 48)
"""
from __future__ import annotations

import os
import secrets


def main() -> None:
    n = int(os.environ.get("JWT_SIGN_HASH_BYTES", "48"))
    print(f"JWT_SIGN_HASH={secrets.token_urlsafe(n)}")


if __name__ == "__main__":
    main()
