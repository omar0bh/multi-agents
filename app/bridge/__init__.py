"""Cloud-to-local execution bridge.

Keep this module free of eager route imports: importing ``app.bridge.security``
or ``app.bridge.actions`` must not drag in FastAPI routers or the manager.
"""

from app.bridge.manager import BridgeManager, bridge_manager

__all__ = ["bridge_manager", "BridgeManager", "bridge_router"]


def __getattr__(name: str):
    if name == "bridge_router":
        from app.bridge.routes import router

        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
