"""Access token verification."""

import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query


def get_expected_token() -> str:
    return os.environ.get("ACCESS_TOKEN", "").strip()


def verify_access_token(
    access_token: Annotated[str | None, Query(alias="access_token")] = None,
    x_access_token: Annotated[str | None, Header(alias="X-Access-Token")] = None,
) -> None:
    expected = get_expected_token()
    if not expected:
        raise HTTPException(status_code=503, detail="ACCESS_TOKEN is not configured")
    got = access_token or x_access_token
    if not got or got != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing access token")


AuthDep = Annotated[None, Depends(verify_access_token)]
