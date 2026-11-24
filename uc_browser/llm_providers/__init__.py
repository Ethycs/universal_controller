"""litellm CustomLLM providers backed by site-automation clients.

The ``uc`` provider routes ``uc/grok`` through the UC service's Grok site
operations; generic non-Grok sites still use their local browser adapter.
Configure the consumer's ``UC_GROK_SERVICE_URL`` before importing the provider.

Typical use (with the service already running)::

    from uc_browser.llm_providers import register_uc_provider
    import litellm

    register_uc_provider()
    resp = litellm.completion(
        model="uc/grok",
        messages=[{"role": "user", "content": "hi"}],
        # Optional — keep continuing the same Grok chat across calls:
        extra_body={"session_id": "my-user-42"},
    )
"""

from uc_browser.llm_providers.uc import (
    UCBrowserCustomLLM,
    get_uc_handler,
    register_uc_provider,
)

__all__ = ["UCBrowserCustomLLM", "get_uc_handler", "register_uc_provider"]
