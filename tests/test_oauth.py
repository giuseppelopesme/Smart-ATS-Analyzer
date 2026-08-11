#!/usr/bin/env python3
"""Tests for the OAuth 2.1 authorization server.

    python3 tests/test_oauth.py

Needs the MCP SDK, because the provider implements its protocol and uses its
models. Skips cleanly when the SDK is absent, so it can sit alongside
test_server.py, which deliberately does not require it.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

try:
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl
except ImportError:  # pragma: no cover
    print("SKIPPED: the mcp SDK is not installed "
          "(pip install -r server/requirements.txt)")
    raise SystemExit(0)

import oauth  # noqa: E402

PASS = FAIL = 0
FAILURES: list = []
PASSWORD = "correct horse battery staple"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  pass  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


def make_client(client_id: str = "test-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="s3cret",
        client_name="Claude",
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="ats:validate",
    )


def make_params() -> AuthorizationParams:
    return AuthorizationParams(
        state="xyz789",
        scopes=["ats:validate"],
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def new_provider(tmp: str, name: str = "state.json") -> oauth.AtsAuthProvider:
    return oauth.AtsAuthProvider(
        base_url="https://ats.example.com",
        password_hash=oauth.hash_password(PASSWORD),
        state_path=os.path.join(tmp, name),
    )


def test_passwords() -> None:
    section("OAuth: passphrase hashing")
    h = oauth.hash_password(PASSWORD)
    check("hash has the expected shape", h.startswith("scrypt$") and h.count("$") == 2, h[:24])
    check("correct passphrase verifies", oauth.verify_password(PASSWORD, h))
    check("wrong passphrase fails", not oauth.verify_password("nope", h))
    check("empty passphrase fails", not oauth.verify_password("", h))
    check("salt differs per hash", oauth.hash_password("a") != oauth.hash_password("a"))
    for bad in ("", "not-a-hash", "scrypt$only-two", "md5$aa$bb", "scrypt$!!!$!!!"):
        check(f"malformed hash rejected: {bad[:16] or 'empty'}",
              not oauth.verify_password("x", bad))


def test_authorization_flow(tmp: str) -> None:
    section("OAuth: authorization code flow")
    p = new_provider(tmp)
    client = make_client()
    asyncio.run(p.register_client(client))
    check("client registered", asyncio.run(p.get_client("test-client")) is not None)
    check("unknown client is not returned",
          asyncio.run(p.get_client("who-dis")) is None)

    url = asyncio.run(p.authorize(client, make_params()))
    check("authorize redirects to the login page", "/oauth/login?txn=" in url, url[:60])
    check("authorize issues no code of its own", "code=" not in url, url[:80])
    txn = url.split("txn=")[1]
    check("pending login names the client",
          p.pending_client_name(txn) == "Claude", str(p.pending_client_name(txn)))

    try:
        p.complete_login(txn, "wrong passphrase")
        check("wrong passphrase issues no code", False, "no exception")
    except ValueError as exc:
        check("wrong passphrase issues no code", "incorrect" in str(exc), str(exc))
    check("transaction survives a failed attempt", txn in p.pending)

    redirect = p.complete_login(txn, PASSWORD)
    check("login redirects to the client", redirect.startswith(
        "https://claude.ai/api/mcp/auth_callback"), redirect[:50])
    check("state is preserved", "state=xyz789" in redirect, redirect)
    check("transaction is consumed", txn not in p.pending)

    code = redirect.split("code=")[1].split("&")[0]
    loaded = asyncio.run(p.load_authorization_code(client, code))
    check("code loads for the right client", loaded is not None)
    check("code carries the PKCE challenge",
          loaded.code_challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    other = make_client("someone-else")
    check("code does not load for another client",
          asyncio.run(p.load_authorization_code(other, code)) is None)

    token = asyncio.run(p.exchange_authorization_code(client, loaded))
    check("access token issued", bool(token.access_token))
    check("refresh token issued", bool(token.refresh_token))
    check("token type is Bearer", token.token_type.lower() == "bearer", token.token_type)

    check("code cannot be replayed",
          asyncio.run(p.load_authorization_code(client, code)) is None)

    access = asyncio.run(p.load_access_token(token.access_token))
    check("access token validates", access is not None)
    check("access token carries the scope",
          access.scopes == ["ats:validate"], str(access.scopes))
    check("forged access token is rejected",
          asyncio.run(p.load_access_token("made-up")) is None)


def test_refresh_and_revoke(tmp: str) -> None:
    section("OAuth: refresh and revocation")
    p = new_provider(tmp, "state2.json")
    client = make_client()
    asyncio.run(p.register_client(client))
    url = asyncio.run(p.authorize(client, make_params()))
    code = p.complete_login(url.split("txn=")[1], PASSWORD).split("code=")[1].split("&")[0]
    loaded = asyncio.run(p.load_authorization_code(client, code))
    first = asyncio.run(p.exchange_authorization_code(client, loaded))

    rt = asyncio.run(p.load_refresh_token(client, first.refresh_token))
    check("refresh token loads", rt is not None)
    check("refresh token is client-scoped",
          asyncio.run(p.load_refresh_token(make_client("other"), first.refresh_token)) is None)

    second = asyncio.run(p.exchange_refresh_token(client, rt, ["ats:validate"]))
    check("refresh yields a new access token",
          second.access_token != first.access_token)
    check("refresh rotates the refresh token",
          second.refresh_token != first.refresh_token)
    check("the old refresh token is dead",
          asyncio.run(p.load_refresh_token(client, first.refresh_token)) is None)

    asyncio.run(p.revoke_token(asyncio.run(p.load_access_token(second.access_token))))
    check("revoked access token stops working",
          asyncio.run(p.load_access_token(second.access_token)) is None)


def test_expiry_and_lockout(tmp: str) -> None:
    section("OAuth: expiry and brute-force lockout")
    p = new_provider(tmp, "state3.json")
    client = make_client()
    asyncio.run(p.register_client(client))

    url = asyncio.run(p.authorize(client, make_params()))
    txn = url.split("txn=")[1]
    p.pending[txn].created = time.time() - (oauth.LOGIN_TXN_TTL + 10)
    try:
        p.complete_login(txn, PASSWORD)
        check("expired login link is refused", False, "no exception")
    except ValueError as exc:
        check("expired login link is refused", "expired" in str(exc), str(exc))

    url = asyncio.run(p.authorize(client, make_params()))
    code = p.complete_login(url.split("txn=")[1], PASSWORD).split("code=")[1].split("&")[0]
    p.auth_codes[code].expires_at = time.time() - 1
    check("expired code does not load",
          asyncio.run(p.load_authorization_code(client, code)) is None)

    url = asyncio.run(p.authorize(client, make_params()))
    code = p.complete_login(url.split("txn=")[1], PASSWORD).split("code=")[1].split("&")[0]
    tok = asyncio.run(p.exchange_authorization_code(
        client, asyncio.run(p.load_authorization_code(client, code))))
    p.access_tokens[tok.access_token].expires_at = int(time.time()) - 1
    check("expired access token is rejected",
          asyncio.run(p.load_access_token(tok.access_token)) is None)

    fresh = new_provider(tmp, "state4.json")
    asyncio.run(fresh.register_client(client))
    url = asyncio.run(fresh.authorize(client, make_params()))
    txn = url.split("txn=")[1]
    for _ in range(oauth.MAX_LOGIN_FAILURES):
        try:
            fresh.complete_login(txn, "wrong")
        except ValueError:
            pass
    try:
        fresh.complete_login(txn, PASSWORD)
        check("lockout blocks even the right passphrase", False, "no exception")
    except ValueError as exc:
        check("lockout blocks even the right passphrase",
              "too many" in str(exc), str(exc))


def test_persistence(tmp: str) -> None:
    section("OAuth: persistence")
    path = os.path.join(tmp, "persist.json")
    p = oauth.AtsAuthProvider(base_url="https://ats.example.com",
                              password_hash=oauth.hash_password(PASSWORD),
                              state_path=path)
    client = make_client("persist-me")
    asyncio.run(p.register_client(client))
    url = asyncio.run(p.authorize(client, make_params()))
    code = p.complete_login(url.split("txn=")[1], PASSWORD).split("code=")[1].split("&")[0]
    tok = asyncio.run(p.exchange_authorization_code(
        client, asyncio.run(p.load_authorization_code(client, code))))

    mode = stat.S_IMODE(os.stat(path).st_mode)
    check("state file is 0600", mode == 0o600, oct(mode))

    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    check("no passphrase or hash in the state file",
          PASSWORD not in raw and "scrypt$" not in raw)

    revived = oauth.AtsAuthProvider(base_url="https://ats.example.com",
                                    password_hash=oauth.hash_password(PASSWORD),
                                    state_path=path)
    check("registered client survives a restart",
          asyncio.run(revived.get_client("persist-me")) is not None)
    check("refresh token survives a restart",
          asyncio.run(revived.load_refresh_token(client, tok.refresh_token)) is not None)
    check("access tokens are NOT persisted",
          asyncio.run(revived.load_access_token(tok.access_token)) is None)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    survived = oauth.AtsAuthProvider(base_url="https://ats.example.com",
                                     password_hash=oauth.hash_password(PASSWORD),
                                     state_path=path)
    check("a corrupt state file does not stop start-up",
          survived.clients == {} and survived.refresh_tokens == {})

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"clients": [{"nonsense": True}], "refresh_tokens": []}, fh)
    tolerant = oauth.AtsAuthProvider(base_url="https://ats.example.com",
                                     password_hash=oauth.hash_password(PASSWORD),
                                     state_path=path)
    check("an unreadable record is skipped, not fatal", tolerant.clients == {})


def main() -> int:
    print("ats-mcp OAuth test suite")
    with tempfile.TemporaryDirectory() as tmp:
        for fn in (test_passwords,):
            fn()
        for fn in (test_authorization_flow, test_refresh_and_revoke,
                   test_expiry_and_lockout, test_persistence):
            try:
                fn(tmp)
            except Exception as exc:  # noqa: BLE001
                global FAIL
                FAIL += 1
                FAILURES.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
