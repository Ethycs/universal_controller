"""litellm CustomLLM providers backed by site-automation clients.

Currently exposes one provider ``uc`` that routes ``uc/<site>`` to a
browser-driven backend. ``uc/grok`` is implemented via the GrokClient
from :mod:`uc_browser.sites.grok`.

Typical use::

    from uc_browser.llm_providers import register_uc_provider
    import litellm

    register_uc_provider()
    resp = litellm.completion(
        model="uc/grok",
        messages=[{"role": "user", "content": "hi"}],
        # Optional — keep continuing the same Grok chat across calls:
        metadata={"session_id": "my-user-42"},
    )
"""

from uc_browser.llm_providers.uc import (
    UCBrowserCustomLLM,
    get_uc_handler,
    register_uc_provider,
)

__all__ = ["UCBrowserCustomLLM", "get_uc_handler", "register_uc_provider"]
