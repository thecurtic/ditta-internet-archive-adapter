"""Every request to the Archive, for both backends, goes through here.

Requests go through the official `internetarchive` library's ArchiveSession,
which sets the library's User-Agent (we append ours) and holds credentials.
We call the session directly rather than through the library's Search
object for two reasons: Search parses the body without looking at the HTTP
status, so a 429 and its Retry-After would be invisible to us; and it
mounts its own retry adapter, which would retry behind our backoff. Here
the library's retries are off and Throttle is the only retry policy.

Throttle also spaces every request (searches, counts, the corpus size) at
least `min_interval_seconds` apart: the Archive asks automated clients to
add delays between requests, not only to cap concurrency.
"""

import logging

import requests
from internetarchive import ArchiveSession
from internetarchive.auth import S3Auth

from .. import TOOL_NAME, __version__
from ..adapter import SourceBusy, SourceError
from .throttle import Throttle, parse_retry_after

log = logging.getLogger(__name__)


class ArchiveHttp:
    """One session, one throttle: the politeness rules in a single place."""

    def __init__(
        self,
        contact_url: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        max_concurrent: int = 2,
        min_interval_seconds: float = 1.0,
        timeout_seconds: float = 20,
        agent: str | None = None,
        session: ArchiveSession | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        # `agent` names the AI model driving the client, which the Archive's
        # bot rules require for AI agents. The deployed adapter is not one and
        # leaves it out; live tests run by a coding agent set it.
        details = f"+{contact_url}" + (f"; {agent}" if agent else "")
        self.user_agent_suffix = f"{TOOL_NAME}/{__version__} ({details})"
        self.session = session or _make_session(self.user_agent_suffix, access_key, secret_key)
        self.throttle = throttle or Throttle(max_concurrent, min_interval=min_interval_seconds)
        self._auth = S3Auth(access_key, secret_key) if access_key and secret_key else None
        self._timeout = timeout_seconds

    def get_json(self, path: str, params: dict) -> object:
        """GET a path on the Archive's host and return the decoded JSON body.

        Raises SourceBusy when the Archive is throttling or failing (and
        starts backing off), SourceError for anything retrying won't fix.
        """
        self.throttle.acquire()
        try:
            response = self.session.get(
                f"{self.session.protocol}//{self.session.host}{path}",
                params=params,
                auth=self._auth,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            self.throttle.failed()
            raise SourceBusy(retry_after=self.seconds_left()) from exc
        finally:
            self.throttle.release()

        if response.status_code == 429 or response.status_code >= 500:
            self.throttle.failed(parse_retry_after(response.headers.get("Retry-After")))
            log.warning("archive returned %s; backing off", response.status_code)
            raise SourceBusy(retry_after=self.seconds_left())
        if response.status_code != 200:
            raise SourceError(f"archive returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise SourceError("archive response was not JSON") from None
        self.throttle.succeeded()
        return body

    def seconds_left(self) -> int:
        return max(1, self.throttle.seconds_until_resume())


def _make_session(user_agent_suffix: str, access_key: str | None, secret_key: str | None) -> ArchiveSession:
    config: dict = {"general": {"user_agent_suffix": user_agent_suffix}}
    if access_key and secret_key:
        config["s3"] = {"access": access_key, "secret": secret_key}
    # max_retries=0: Throttle owns every retry decision (see module docstring).
    return ArchiveSession(config=config, http_adapter_kwargs={"max_retries": 0})
