"""OAuth 2.1 authorization server for the ATS MCP endpoint.

claude.ai's custom-connector UI speaks OAuth 2.1 and nothing else — there is no
field for an API key — so authenticating properly means being an authorization
server, not merely checking a header.

What the SDK already does, and this therefore does not: mounting
``/authorize``, ``/token``, ``/register`` and the metadata documents,
verifying the PKCE ``code_verifier`` against the stored S256 challenge, and
checking that ``redirect_uri`` at the token endpoint matches the one used at
the authorization endpoint.

What this module supplies:

* **A real user-authentication step.** ``authorize`` does not mint a code on
  the spot; it parks the request and sends the browser to a login page. A code
  is issued only after a passphrase is verified. Without this an "authorization
  server" authorizes anybody who can reach it.
* **Storage.** Registered clients and refresh tokens persist to a 0600 JSON
  file, so a restart does not silently unpair the connector. Authorization
  codes and access tokens stay in memory, where their short lifetimes belong.
* **Token minting, rotation and revocation.**

The passphrase is stored as a scrypt hash and compared in constant time.
Generate one with::

    python3 server/oauth.py --hash-password
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

__all__ = ["AtsAuthProvider", "hash_password", "verify_password", "PendingLogin"]

AUTH_CODE_TTL = 300           # 5 minutes, per OAuth 2.1 guidance
ACCESS_TOKEN_TTL = 3600       # 1 hour
REFRESH_TOKEN_TTL = 30 * 86400
LOGIN_TXN_TTL = 600
MAX_LOGIN_FAILURES = 8
LOGIN_LOCKOUT_SECONDS = 300

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
# scrypt needs 128 * N * r bytes; OpenSSL's default ceiling is 32 MB, which is
# exactly what these parameters ask for, so it must be raised explicitly or
# every hash fails with "memory limit exceeded".
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2


# --------------------------------------------------------------------------
# Passphrase handling
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return ``scrypt$<salt>$<hash>``, both base64url without padding."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                            maxmem=SCRYPT_MAXMEM)
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return f"scrypt${enc(salt)}${enc(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a passphrase against a stored scrypt hash."""
    try:
        scheme, salt_b64, hash_b64 = encoded.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False

    def dec(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    try:
        salt, expected = dec(salt_b64), dec(hash_b64)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                                dklen=len(expected), maxmem=SCRYPT_MAXMEM)
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------
# Pending login
# --------------------------------------------------------------------------


@dataclass
class PendingLogin:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created > LOGIN_TXN_TTL


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


class AtsAuthProvider(OAuthAuthorizationServerProvider):
    """A single-user OAuth 2.1 authorization server."""

    def __init__(
        self,
        base_url: str,
        password_hash: str,
        state_path: Optional[str] = None,
        subject: str = "owner",
    ):
        self.base_url = base_url.rstrip("/")
        self.password_hash = password_hash
        self.state_path = state_path
        self.subject = subject

        self.clients: Dict[str, OAuthClientInformationFull] = {}
        self.refresh_tokens: Dict[str, RefreshToken] = {}
        # Short-lived, so deliberately not persisted.
        self.auth_codes: Dict[str, AuthorizationCode] = {}
        self.access_tokens: Dict[str, AccessToken] = {}
        self.pending: Dict[str, PendingLogin] = {}

        self._failures: List[float] = []
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("clients", []):
            try:
                info = OAuthClientInformationFull.model_validate(raw)
                self.clients[info.client_id] = info
            except Exception:  # noqa: BLE001 - a bad record must not block start-up
                continue
        now = time.time()
        for raw in data.get("refresh_tokens", []):
            try:
                tok = RefreshToken.model_validate(raw)
            except Exception:  # noqa: BLE001
                continue
            if tok.expires_at and tok.expires_at < now:
                continue
            self.refresh_tokens[tok.token] = tok

    def _save(self) -> None:
        if not self.state_path:
            return
        payload = {
            "clients": [c.model_dump(mode="json", exclude_none=True)
                        for c in self.clients.values()],
            "refresh_tokens": [t.model_dump(mode="json", exclude_none=True)
                               for t in self.refresh_tokens.values()],
        }
        tmp = f"{self.state_path}.tmp"
        # Create with 0600 from the outset: the file holds refresh tokens, so
        # it must never exist even briefly with looser permissions.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, self.state_path)

    # -- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info
        self._save()

    # -- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the login page.

        Issuing a code here instead would authorize anyone who can reach the
        endpoint, which is the whole thing OAuth is supposed to prevent.
        """
        self._sweep()
        txn = secrets.token_urlsafe(24)
        self.pending[txn] = PendingLogin(client=client, params=params)
        return f"{self.base_url}/oauth/login?txn={txn}"

    def complete_login(self, txn: str, password: str) -> str:
        """Verify the passphrase and return the redirect back to the client.

        Raises ValueError with a message safe to display.
        """
        self._sweep()
        if self._locked_out():
            raise ValueError(
                "too many failed attempts; wait a few minutes and try again"
            )

        pending = self.pending.get(txn)
        if pending is None or pending.expired:
            self.pending.pop(txn, None)
            raise ValueError("this login link has expired — start again from Claude")

        if not verify_password(password, self.password_hash):
            self._failures.append(time.time())
            raise ValueError("incorrect passphrase")

        # Success: burn the transaction and the failure counter.
        self.pending.pop(txn, None)
        self._failures.clear()

        code = secrets.token_urlsafe(32)
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=pending.client.client_id,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            expires_at=time.time() + AUTH_CODE_TTL,
            scopes=pending.params.scopes or [],
            code_challenge=pending.params.code_challenge,
            resource=pending.params.resource,
            subject=self.subject,
        )
        return construct_redirect_uri(
            str(pending.params.redirect_uri),
            code=code,
            state=pending.params.state,
        )

    def pending_client_name(self, txn: str) -> Optional[str]:
        pending = self.pending.get(txn)
        if pending is None or pending.expired:
            return None
        return pending.client.client_name or pending.client.client_id

    # -- codes and tokens ---------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        code = self.auth_codes.get(authorization_code)
        if code is None:
            return None
        if code.client_id != client.client_id:
            return None
        if code.expires_at and code.expires_at < time.time():
            self.auth_codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: an authorization code must never be redeemable twice.
        self.auth_codes.pop(authorization_code.code, None)
        return self._issue(client, authorization_code.scopes,
                           resource=authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        tok = self.refresh_tokens.get(refresh_token)
        if tok is None or tok.client_id != client.client_id:
            return None
        if tok.expires_at and tok.expires_at < time.time():
            self.refresh_tokens.pop(refresh_token, None)
            self._save()
            return None
        return tok

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: List[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token is consumed as the new pair is
        # issued, so a leaked one is useful at most once.
        self.refresh_tokens.pop(refresh_token.token, None)
        granted = scopes or refresh_token.scopes
        return self._issue(client, granted)

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        tok = self.access_tokens.get(token)
        if tok is None:
            return None
        if tok.expires_at and tok.expires_at < time.time():
            self.access_tokens.pop(token, None)
            return None
        return tok

    async def revoke_token(self, token: Any) -> None:
        value = getattr(token, "token", token)
        self.access_tokens.pop(value, None)
        if self.refresh_tokens.pop(value, None) is not None:
            self._save()

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:
        raise NotImplementedError("identity assertion grant is not enabled")

    # -- internals -----------------------------------------------------------

    def _issue(
        self,
        client: OAuthClientInformationFull,
        scopes: List[str],
        resource: Optional[str] = None,
    ) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)

        self.access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=list(scopes),
            expires_at=int(now + ACCESS_TOKEN_TTL),
            subject=self.subject,
            resource=resource,
        )
        self.refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=list(scopes),
            expires_at=int(now + REFRESH_TOKEN_TTL),
            subject=self.subject,
        )
        self._sweep()
        self._save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    def _sweep(self) -> None:
        now = time.time()
        for store in (self.auth_codes, self.access_tokens, self.refresh_tokens):
            for key in [k for k, v in store.items()
                        if getattr(v, "expires_at", None) and v.expires_at < now]:
                store.pop(key, None)
        for key in [k for k, v in self.pending.items() if v.expired]:
            self.pending.pop(key, None)

    def _locked_out(self) -> bool:
        cutoff = time.time() - LOGIN_LOCKOUT_SECONDS
        self._failures = [t for t in self._failures if t > cutoff]
        return len(self._failures) >= MAX_LOGIN_FAILURES


# --------------------------------------------------------------------------
# Login page
# --------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — ATS validator</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 26rem; margin: 0 auto; padding: 3rem 1.25rem; }}
  h1 {{ font-size: 1.25rem; margin-bottom: .25rem; }}
  p.sub {{ color: #666; margin-top: 0; }}
  input[type=password] {{ width: 100%; padding: .8rem; font-size: 1rem;
    border: 1px solid #8884; border-radius: .5rem; background: #8881; }}
  button {{ margin-top: 1rem; width: 100%; padding: .85rem; font-size: 1rem;
    font-weight: 600; border: 0; border-radius: .5rem; background: #0b6cff; color: #fff; }}
  .err {{ color: #c00; font-weight: 600; }}
  .note {{ font-size: .85rem; color: #666; margin-top: 1.5rem; }}
</style></head><body>
<h1>Sign in</h1>
<p class="sub">{client} is asking to use your ATS validator.</p>
{error}
<form method="post" action="/oauth/login">
  <input type="hidden" name="txn" value="{txn}">
  <input type="password" name="password" placeholder="Passphrase"
         autocomplete="current-password" autofocus required>
  <button type="submit">Sign in and authorize</button>
</form>
<p class="note">Approving grants this application access to validate documents
you send it. You can revoke it by removing the connector, or by deleting the
server's state file.</p>
</body></html>
"""


def login_page(txn: str, client_name: str, error: Optional[str] = None) -> str:
    return LOGIN_PAGE.format(
        txn=_escape(txn),
        client=_escape(client_name or "An application"),
        error=f'<p class="err">{_escape(error)}</p>' if error else "",
    )


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


if __name__ == "__main__":
    import getpass
    import sys

    if "--hash-password" in sys.argv:
        pw = getpass.getpass("Passphrase: ")
        again = getpass.getpass("Repeat: ")
        if pw != again:
            print("error: passphrases do not match", file=sys.stderr)
            raise SystemExit(2)
        if len(pw) < 12:
            print("error: use at least 12 characters — this is the only thing "
                  "between the internet and your documents", file=sys.stderr)
            raise SystemExit(2)
        print(f"ATS_OAUTH_PASSWORD_HASH={hash_password(pw)}")
        raise SystemExit(0)

    print(__doc__)
