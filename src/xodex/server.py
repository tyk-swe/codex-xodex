"""Compatibility imports; HTTP adapters and client have independent modules."""
from .client import EngineClient
from .gateway import INSTRUCTIONS, PROTOCOLS, gateway_app, tool_result
from .supervisor import engine_app

__all__ = ["EngineClient", "INSTRUCTIONS", "PROTOCOLS", "engine_app", "gateway_app", "tool_result"]
