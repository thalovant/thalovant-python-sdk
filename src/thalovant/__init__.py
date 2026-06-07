"""Thalovant Python SDK."""

from .agent import AsyncThalovantAgent, ThalovantAgent
from .client import AsyncThalovantClient, ThalovantClient
from .context import build_client_context
from .conversation import AsyncThalovantConversation, ThalovantConversation
from .errors import (
    ThalovantConnectionError,
    ThalovantError,
    ThalovantIdentityError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)
from .events import (
    EVENT_INTENT_FAILURE,
    EVENT_POLICY_DENIED,
    EVENT_RECOGNIZER_LOOP_UTTERANCE,
    EVENT_SPEAK,
    EVENT_UTTERANCE_HANDLED,
    EventHandler,
    EventPredicate,
    ThalovantEvent,
)
from .identity import ThalovantIdentity
from .models import (
    ThalovantDoctorCheck,
    ThalovantDoctorReport,
    ThalovantHealth,
    ThalovantReply,
)
from .protocols import HubDataPlaneEndpoints, HubProtocol, HubProtocolSettings
from .rich import ThalovantDisplayItem, strip_ssml
from .subscriptions import ThalovantSubscription

__version__ = "0.4.2"

__all__ = [
    "AsyncThalovantAgent",
    "AsyncThalovantClient",
    "AsyncThalovantConversation",
    "EVENT_INTENT_FAILURE",
    "EVENT_POLICY_DENIED",
    "EVENT_RECOGNIZER_LOOP_UTTERANCE",
    "EVENT_SPEAK",
    "EVENT_UTTERANCE_HANDLED",
    "EventHandler",
    "EventPredicate",
    "HubDataPlaneEndpoints",
    "HubProtocol",
    "HubProtocolSettings",
    "ThalovantDisplayItem",
    "ThalovantAgent",
    "ThalovantClient",
    "ThalovantConnectionError",
    "ThalovantConversation",
    "ThalovantDoctorCheck",
    "ThalovantDoctorReport",
    "ThalovantError",
    "ThalovantEvent",
    "ThalovantHealth",
    "ThalovantIdentity",
    "ThalovantIdentityError",
    "ThalovantReply",
    "ThalovantRuntimeError",
    "ThalovantSubscription",
    "ThalovantTimeoutError",
    "build_client_context",
    "strip_ssml",
]
