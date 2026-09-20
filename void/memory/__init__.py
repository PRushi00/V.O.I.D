"""Persistent assistant memory (V2.0 P3): encrypted, selective, retrievable, forgettable.

Memory is DATA. It can inform a response; it can never authorize, approve or configure
anything. See ``service.MemoryService`` for the API and ``policy`` for the write rules.
"""
from void.memory.crypto import MemoryUnavailable
from void.memory.service import ForgetOutcome, MemoryService, Settings, WriteResult

__all__ = ["MemoryService", "MemoryUnavailable", "Settings", "WriteResult", "ForgetOutcome"]
