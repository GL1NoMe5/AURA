"""AURA local HTTP/WebSocket API package.

The FastAPI app is loaded lazily so importing aura_api (or its schemas)
from aura_agents never creates an import cycle or wires providers at
import time.
"""
from .config import ApiSettings
from .manager import IncidentManager
from .orchestration import (
    EvidenceBundle,
    OrchestrationOutcome,
    Orchestrator,
    SpecialistFinding,
    SynthesisProposal,
    UnavailableOrchestrator,
)

__all__ = [
    "app",
    "create_app",
    "ApiSettings",
    "IncidentManager",
    "EvidenceBundle",
    "OrchestrationOutcome",
    "Orchestrator",
    "SpecialistFinding",
    "SynthesisProposal",
    "UnavailableOrchestrator",
]


def __getattr__(name: str):
    if name in ("app", "create_app"):
        import importlib

        _app_module = importlib.import_module(".app", __name__)
        return getattr(_app_module, name)
    raise AttributeError(f"module 'aura_api' has no attribute {name!r}")
