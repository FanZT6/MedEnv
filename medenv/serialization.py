"""Serialization helpers for experiment artifacts."""


def redact_secrets(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if any(word in key.lower() for word in ("api_key", "password", "secret", "authorization"))
            else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value
