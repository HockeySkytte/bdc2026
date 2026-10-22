"""Shared-password gate for the app.

The machinery is the same as the event app in ``challenge/``:

* one shared password (``APP_PASSWORD``, never committed) unlocks a signed
  cookie - there is no user account involved;
* the password is part of the signed label, so rotating it invalidates every
  cookie already handed out;
* the token is an HMAC of that label, so a cookie cannot be forged without the
  secret.

One rule is deliberately inverted.  With no password configured this app is
**open**, where the event app is locked.  A public analysis site should come up
working on a fresh deploy; set ``APP_PASSWORD`` and the gate turns on.
"""

from __future__ import annotations

import hashlib
import hmac

from flask import current_app, request


def gate_label(password: str) -> str:
    """The label signed into the unlock cookie."""
    return f"unlock:{password}"


def label_token(secret: str, label: str) -> str:
    """HMAC-SHA256 of ``label``, hex encoded."""
    return hmac.new(secret.encode("utf-8"), label.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_label_token(secret: str, label: str, value: str) -> bool:
    """Constant-time check of a :func:`label_token` value."""
    return hmac.compare_digest(label_token(secret, label), (value or "").strip())


def gate_allows(configured_password: str, secret: str, cookie: str | None) -> bool:
    """Decide whether a request may see the page.

    Split out from the request handling so the rule is directly testable without
    a Flask context.
    """
    password = (configured_password or "").strip()
    if not password:
        # No password set: the site is public.
        return True
    if not cookie:
        return False
    return verify_label_token(secret, gate_label(password), cookie)


def is_unlocked() -> bool:
    config = current_app.config["SETTINGS"]
    return gate_allows(
        config.password,
        config.secret_key,
        request.cookies.get(config.cookie_name),
    )


def issue_unlock(response):
    """Attach the unlock cookie to a response."""
    config = current_app.config["SETTINGS"]
    response.set_cookie(
        config.cookie_name,
        label_token(config.secret_key, gate_label(config.password.strip())),
        max_age=config.cookie_max_age,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return response


def clear_unlock(response):
    config = current_app.config["SETTINGS"]
    response.delete_cookie(config.cookie_name, httponly=True, samesite="Lax")
    return response
