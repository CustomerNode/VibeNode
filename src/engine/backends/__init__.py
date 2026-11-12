"""Backend abstractions for AI agent SDKs.

This package defines the abstract interfaces that decouple VibeNode's
session orchestration layer (SessionManager) from any specific AI SDK.

Key exports:
    AgentSDK        - Abstract base for AI agent backends
    SessionOptions  - Backend-agnostic session configuration
    PermissionAction - ALLOW / DENY enum
    PermissionResult - Result of a permission check
    VibeNodeMessage - Normalized message from any backend
    MessageKind     - Message type enum
    BlockKind       - Content block type enum
    ChatStore       - Abstract base for session persistence
"""

from engine.backends.base import (
    AgentSDK,
    SessionOptions,
    PermissionAction,
    PermissionResult,
)
from engine.backends.messages import (
    VibeNodeMessage,
    MessageKind,
    BlockKind,
)
from engine.backends.chat_store import ChatStore

__all__ = [
    "AgentSDK",
    "SessionOptions",
    "PermissionAction",
    "PermissionResult",
    "VibeNodeMessage",
    "MessageKind",
    "BlockKind",
    "ChatStore",
]
