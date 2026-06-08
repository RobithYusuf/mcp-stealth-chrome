"""CapSolver integration — optional Turnstile/reCAPTCHA solver via HTTP API.

Not a hard dependency. Activated only when CAPSOLVER_KEY env var is set.
Token is returned to the caller; user is responsible for injecting into the
correct form field (Turnstile uses hidden input named `cf-turnstile-response`).
"""
from __future__ import annotations

import asyncio
import os
from typing import Literal

import httpx

CAPSOLVER_API = "https://api.capsolver.com"


class CapSolverError(RuntimeError):
    pass


async def solve(
    task_type: Literal["AntiTurnstileTaskProxyLess", "ReCaptchaV2TaskProxyLess",
                        "ReCaptchaV3TaskProxyLess", "HCaptchaTaskProxyLess"],
    website_url: str,
    website_key: str,
    api_key: str | None = None,
    metadata: dict | None = None,
    timeout: float = 120.0,
) -> str:
    """Create a CapSolver task and poll until solved. Returns the token string.

    Raises CapSolverError on failure or timeout.
    """
    api_key = api_key or os.environ.get("CAPSOLVER_KEY")
    if not api_key:
        raise CapSolverError(
            "CAPSOLVER_KEY env var not set. Get one at https://capsolver.com"
        )

    payload_task: dict = {
        "type": task_type,
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if metadata:
        payload_task["metadata"] = metadata

    async with httpx.AsyncClient(timeout=30.0) as client:
        create = await client.post(
            f"{CAPSOLVER_API}/createTask",
            json={"clientKey": api_key, "task": payload_task},
        )
        # A non-JSON body (HTML error page, gateway 502, rate-limit text) would
        # raise a bare JSONDecodeError that the caller's `except CapSolverError`
        # doesn't catch — normalise it into a CapSolverError.
        try:
            data = create.json()
        except Exception as e:
            raise CapSolverError(
                f"createTask HTTP {create.status_code}: {create.text[:200]}"
            ) from e
        if data.get("errorId"):
            raise CapSolverError(f"createTask: {data.get('errorDescription', data)}")
        task_id = data.get("taskId")
        if not task_id:
            raise CapSolverError(f"createTask: missing taskId in {data}")

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3.0)
            res = await client.post(
                f"{CAPSOLVER_API}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            try:
                rdata = res.json()
            except Exception as e:
                raise CapSolverError(
                    f"getTaskResult HTTP {res.status_code}: {res.text[:200]}"
                ) from e
            if rdata.get("errorId"):
                raise CapSolverError(f"getTaskResult: {rdata.get('errorDescription', rdata)}")
            if rdata.get("status") == "ready":
                sol = rdata.get("solution", {})
                return sol.get("token") or sol.get("gRecaptchaResponse") or ""

    raise CapSolverError(f"Task {task_id} did not resolve within {timeout}s")
