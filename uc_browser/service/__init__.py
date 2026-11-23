"""Independent local browser service; unprefixed HTTP wire contract v0."""

WIRE_VERSION = 0


def __getattr__(name):
    if name == "ServiceClient":
        from .client import ServiceClient
        return ServiceClient
    raise AttributeError(name)


__all__ = ["ServiceClient", "WIRE_VERSION"]
