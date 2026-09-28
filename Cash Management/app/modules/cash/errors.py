"""Errors raised by the cash management services.

Services raise these instead of HTTP exceptions so they can be reused outside
the API (batch jobs, other modules). The router maps them to HTTP responses.
"""


class CashError(Exception):
    status_code = 400


class CashNotFound(CashError):
    status_code = 404


class CashValidationError(CashError):
    """Input that cannot be processed (422), e.g. a statement that does not foot."""

    status_code = 422

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        self.problems = problems or [message]
        super().__init__(message if not problems else message + ": " + "; ".join(problems))


class CashConflict(CashError):
    """State conflict (409): duplicate with different content, closed session, over-match..."""

    status_code = 409
