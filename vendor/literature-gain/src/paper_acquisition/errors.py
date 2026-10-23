class AcquisitionError(Exception):
    """Base application error. Messages must never contain credentials."""


class MissingCredentials(AcquisitionError):
    pass


class AuthenticationError(AcquisitionError):
    pass


class RateLimitError(AcquisitionError):
    pass


class RemoteError(AcquisitionError):
    pass


class NetworkError(AcquisitionError):
    pass


class QueryError(AcquisitionError):
    pass


class ResponseError(AcquisitionError):
    pass


class DocumentationBlocked(AcquisitionError):
    pass


class OfflineCacheMiss(AcquisitionError):
    pass
