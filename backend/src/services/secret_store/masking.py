def mask_secret(value: str | None) -> str | None:
    """Never returns anything closer to the real value than the last 4
    characters -- the only masking rule this project has, shared by every
    SecretStore implementation and by services/provider_status.py so a
    dashboard can never accidentally show more of a key than the credential
    forms themselves do."""
    if not value:
        return None
    if len(value) <= 4:
        return "•" * len(value)
    return "••••••••" + value[-4:]
