"""A small client for the three Freelancer.com API calls that bidding needs.

It replaces the freelancersdk package, which set no timeouts and installed Sphinx as a runtime dependency.
Calls go to the same endpoints with the same Freelancer-OAuth-V1 header. fdm.bids turns a failure into a
message that names the step that failed.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

API = "https://www.freelancer.com/api/"
USER_AGENT = "Shortlister/0.1 (+python httpx)"
# A stalled call would otherwise hold one of the portal's threads indefinitely.
TIMEOUT_SECONDS = 30


class APIError(Exception):
    """A call failed. The message says why, fit to show: Freelancer.com's own reason when it gave one.

    `unsure` means Freelancer.com may have acted on the call anyway: the request was sent, but no clear answer
    came back.
    """

    def __init__(self, message: str, unsure: bool = False):
        super().__init__(message)
        self.unsure = unsure


class Freelancer:
    """The API, called as the owner of an OAuth token. Use it in a `with` block so its connections close."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=API, headers={"Freelancer-OAuth-V1": token, "User-Agent": USER_AGENT},
                                    timeout=TIMEOUT_SECONDS, transport=transport)

    def __enter__(self) -> Freelancer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._client.close()

    def get_self(self) -> dict:
        """The account the token belongs to."""
        return self._call("GET", "users/0.1/self/")

    def get_project(self, project_id: int) -> dict:
        """One project's live state."""
        return self._call("GET", f"projects/0.1/projects/{project_id}/")

    def place_bid(self, **bid: object) -> dict:
        """Place a bid. Once this returns, it is placed."""
        return self._call("POST", "projects/0.1/bids/", json=bid)

    def _call(self, method: str, path: str, **kwargs: object) -> dict:
        """The reply's `result`, or APIError saying why there is none."""
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # Only a failure to connect proves the request was never sent; a lost reply may follow a placed bid.
            unsure = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            raise APIError(f"could not reach Freelancer.com ({str(exc) or type(exc).__name__}).", unsure) from exc
        try:
            body = response.json()
        except ValueError:  # an HTML error page from a proxy, say
            body = None
        body = body if isinstance(body, dict) else {}
        result = body.get("result")
        if response.is_success and isinstance(result, dict):
            return result
        reason = body.get("message")
        log.warning("Freelancer.com %s /api/%s failed: HTTP %s, %s (%s, request %s)", method, path,
                    response.status_code, reason, body.get("error_code"), body.get("request_id"))
        unsure = not 400 <= response.status_code < 500  # a 4xx is a refusal; a garbled 2xx or a 5xx is not clear
        if not response.is_success and isinstance(reason, str) and reason.strip():
            raise APIError(reason.strip(), unsure)
        status = "" if response.is_success else f" (HTTP {response.status_code})"
        raise APIError(f"Freelancer.com sent an unexpected reply{status}.", unsure)
