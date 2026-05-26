"""
Token Manager for GPT4IFX API
==============================

Automatically checks whether the current ``LLAMA_TOKEN`` (JWT) has expired
and, if so, fetches a fresh one from the ``/auth/token`` endpoint using
Basic auth with IFX credentials.

The credentials are read from the ``.env`` file (``IFX_USERNAME`` /
``IFX_PASSWORD``) **or** from environment variables.  The refreshed token
is written back to both ``os.environ`` and the ``.env`` file so that
subsequent scripts pick it up without manual intervention.

Usage — standalone
------------------
    python token_manager.py          # check & refresh if needed
    python token_manager.py --force  # always fetch a new token

Usage — as a library
--------------------
    from token_manager import ensure_valid_token

    token = ensure_valid_token()     # returns a valid JWT string
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv, set_key

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent          # .../HybridRAG/code
_REPO_ROOT = _SCRIPT_DIR.parents[2]                    # repo root
_ENV_FILE = _REPO_ROOT / "env" / ".env"
_CA_BUNDLE = _SCRIPT_DIR / "ca-bundle.crt"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_TOKEN_URL = "https://gpt4ifx.icp.infineon.com/auth/token"
_EXPIRY_BUFFER_MINUTES = 5          # treat the token as expired N min early
_HTTP_TIMEOUT = 30                  # seconds

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JWT helpers (no external JWT library needed — we only inspect the payload)
# ---------------------------------------------------------------------------

def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload (2nd segment) of a JWT without verification."""
    try:
        payload_b64 = token.split(".")[1]
        # Fix base64 padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        return json.loads(base64.b64decode(payload_b64))
    except (IndexError, json.JSONDecodeError, Exception) as exc:
        logger.debug("Cannot decode JWT payload: %s", exc)
        return {}


def is_token_expired(token: str, buffer_minutes: int = _EXPIRY_BUFFER_MINUTES) -> bool:
    """Return *True* if the token is missing, malformed, or will expire
    within *buffer_minutes* from now."""
    if not token:
        return True
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    if exp is None:
        logger.info("JWT has no 'exp' claim — treating as non-expiring.")
        return False
    expiry_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    remaining = expiry_dt - now
    logger.debug(
        "Token expires at %s (in %s). Buffer = %d min.",
        expiry_dt.isoformat(), remaining, buffer_minutes,
    )
    return remaining < timedelta(minutes=buffer_minutes)


def get_token_info(token: str) -> dict:
    """Return a human-readable dict with token timing information."""
    payload = _decode_jwt_payload(token)
    info: dict = {}
    now = datetime.now(tz=timezone.utc)
    for key in ("iat", "exp", "nbf"):
        ts = payload.get(key)
        if ts is not None:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            info[key] = dt.isoformat()
    if "exp" in info:
        exp_dt = datetime.fromisoformat(info["exp"])
        remaining = exp_dt - now
        info["remaining"] = str(remaining)
        info["expired"] = remaining.total_seconds() <= 0
    return info


# ---------------------------------------------------------------------------
# Token fetching
# ---------------------------------------------------------------------------

def fetch_new_token(
    username: str,
    password: str,
    *,
    token_url: str = _TOKEN_URL,
    ca_bundle: Optional[str | Path] = None,
) -> str:
    """Fetch a fresh JWT from the GPT4IFX ``/auth/token`` endpoint.

    Uses HTTP Basic authentication with the provided IFX credentials.
    Works on both Windows and Linux.

    Returns:
        The raw JWT string.

    Raises:
        RuntimeError: On HTTP errors or unexpected responses.
    """
    verify: str | bool = True
    ca_path = Path(ca_bundle) if ca_bundle else _CA_BUNDLE
    if ca_path.exists():
        verify = str(ca_path)

    logger.info("Requesting new token from %s (user=%s)", token_url, username)

    with httpx.Client(verify=verify, timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
        resp = client.get(token_url, auth=(username, password))

    if resp.status_code == 401:
        raise RuntimeError(
            "Authentication failed (401). Please check IFX_USERNAME / "
            "IFX_PASSWORD in your .env file."
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token request failed with HTTP {resp.status_code}: "
            f"{resp.text[:300]}"
        )

    # Response may be JSON {"token": "..."} or raw text
    content_type = resp.headers.get("content-type", "")
    if "json" in content_type:
        data = resp.json()
        token = data.get("token") or data.get("access_token") or data.get("jwt")
        if not token:
            # Fallback: take the first string value that looks like a JWT
            for v in data.values():
                if isinstance(v, str) and v.count(".") == 2:
                    token = v
                    break
        if not token:
            raise RuntimeError(
                f"Could not extract token from JSON response: {data}"
            )
    else:
        token = resp.text.strip()

    if not token or token.count(".") != 2:
        raise RuntimeError(f"Response does not look like a JWT: {token[:80]}…")

    logger.info("New token obtained — expires: %s", get_token_info(token).get("exp", "?"))
    return token


# ---------------------------------------------------------------------------
# .env persistence
# ---------------------------------------------------------------------------

def _update_env_file(key: str, value: str, env_path: Path = _ENV_FILE) -> None:
    """Write/update a key in the ``.env`` file."""
    if env_path.exists():
        set_key(str(env_path), key, value)
        logger.debug("Updated %s in %s", key, env_path)
    else:
        logger.warning(".env file not found at %s — skipping file update.", env_path)


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def ensure_valid_token(
    *,
    force_refresh: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
    env_path: Optional[Path | str] = None,
) -> str:
    """Return a valid ``LLAMA_TOKEN``, refreshing it if expired.

    Steps:
        1. Load ``.env`` so ``LLAMA_TOKEN``, ``IFX_USERNAME``, and
           ``IFX_PASSWORD`` are available.
        2. Check whether the current token is still valid.
        3. If expired (or *force_refresh*), fetch a new one via Basic auth.
        4. Write the new token back to ``os.environ`` **and** the ``.env``
           file.

    Parameters
    ----------
    force_refresh : bool
        Skip the expiry check and always fetch a new token.
    username, password : str, optional
        Override credentials (defaults to env vars ``IFX_USERNAME`` /
        ``IFX_PASSWORD``).
    env_path : Path or str, optional
        Explicit path to ``.env`` (default: ``<repo>/env/.env``).

    Returns
    -------
    str
        A valid JWT token.
    """
    _env = Path(env_path) if env_path else _ENV_FILE
    load_dotenv(_env, override=True)

    current_token = os.environ.get("LLAMA_TOKEN", "")

    if not force_refresh and not is_token_expired(current_token):
        info = get_token_info(current_token)
        if "exp" in info:
            logger.info(
                "Token is still valid (expires %s, remaining %s).",
                info["exp"], info.get("remaining", "?"),
            )
        else:
            logger.info("Token is valid (no expiry — non-expiring service token).")
        return current_token

    reason = "forced refresh" if force_refresh else "expired / missing"
    logger.info("Token needs refresh (%s). Fetching new token…", reason)

    user = username or os.environ.get("IFX_USERNAME", "")
    pwd = password or os.environ.get("IFX_PASSWORD", "")

    if not user or not pwd:
        raise RuntimeError(
            "Cannot refresh token: IFX_USERNAME and IFX_PASSWORD must be "
            "set in the .env file or passed as arguments.\n"
            "Add the following to your env/.env file:\n"
            "  IFX_USERNAME=your_windows_username\n"
            "  IFX_PASSWORD=your_windows_password"
        )

    new_token = fetch_new_token(user, pwd)

    # Persist
    os.environ["LLAMA_TOKEN"] = new_token
    _update_env_file("LLAMA_TOKEN", new_token, _env)

    return new_token


# ---------------------------------------------------------------------------
# Thread-safe singleton access — THE recommended entry point for all services
# ---------------------------------------------------------------------------
_token_lock = threading.Lock()


def get_token() -> str:
    """Return a valid GPT4IFX JWT, fetching a new one only if expired.

    Decodes the current token and checks expiry.  If valid, returns it
    immediately; if expired or missing, fetches a fresh one.

    Thread-safe.  All services (MCP server, parsers, pipelines) should
    call this instead of reading ``LLAMA_TOKEN`` from the environment
    directly.
    """
    with _token_lock:
        return ensure_valid_token()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Check / refresh the LLAMA_TOKEN for GPT4IFX API."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Always fetch a new token, even if the current one is valid.",
    )
    parser.add_argument(
        "--info", action="store_true",
        help="Only print token info without refreshing.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    load_dotenv(_ENV_FILE, override=True)

    if args.info:
        token = os.environ.get("LLAMA_TOKEN", "")
        if not token:
            print("No LLAMA_TOKEN found in environment / .env file.")
            sys.exit(1)
        info = get_token_info(token)
        for k, v in info.items():
            print(f"  {k}: {v}")
        sys.exit(0)

    try:
        token = ensure_valid_token(force_refresh=args.force)
        info = get_token_info(token)
        print("Token is valid:")
        for k, v in info.items():
            print(f"  {k}: {v}")
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
