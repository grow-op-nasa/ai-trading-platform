"""Exceptions for the broker capability."""

from __future__ import annotations


class BrokerError(Exception):
    """Base class for broker-related failures."""


class BrokerAuthenticationError(BrokerError):
    """Raised when a broker rejects the provided credentials, or none
    were supplied at all."""


class BrokerConnectionError(BrokerError):
    """Raised when a broker can't be reached -- network failure,
    timeout, or a non-authentication HTTP error."""
