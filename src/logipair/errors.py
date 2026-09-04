class LogiPairError(Exception):
    """Base error for expected LogiPair failures."""


class TransportError(LogiPairError):
    pass


class OwnershipError(TransportError):
    pass


class ProtocolError(LogiPairError):
    pass


class HidApiVersionError(LogiPairError):
    pass
