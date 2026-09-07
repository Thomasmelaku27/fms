"""Domain errors raised by services; the API maps them to HTTP responses.

* ``ConflictError`` (409): the operation is not valid in the resource's current
  state (already posted, already reversed, period already closed, ...).
* ``UnprocessableError`` (422): the data itself violates a business rule.
"""


class ServiceError(Exception):
    status_code = 400


class ConflictError(ServiceError):
    status_code = 409


class UnprocessableError(ServiceError):
    status_code = 422
