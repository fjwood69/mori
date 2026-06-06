"""hermes-mori-provider — mori MemoryProvider plugin for hermes-agent.

Entry point used by hermes-agent's plugin loader::

    from hermes_mori_provider import register

    register(ctx)

The ``ctx`` object must expose ``register_memory_provider(provider)``.
"""

from .provider import MoriMemoryProvider

__all__ = ["MoriMemoryProvider", "register"]
__version__ = "0.3.0"


def register(ctx: object) -> None:
    """Register the mori MemoryProvider with the hermes-agent plugin context."""
    ctx.register_memory_provider(MoriMemoryProvider())  # type: ignore[attr-defined]
