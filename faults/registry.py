"""Fault registry: type name -> class, and metadata helpers used by docs and scenarios."""

from __future__ import annotations

import numpy as np

from faults.base import Fault, FaultSpec
from faults.ops import (
    BadConfigDeploy,
    CacheFailure,
    DbConnExhaustion,
    DiskFull,
    LatencyInjection,
    MemoryLeak,
    ProcessCrash,
)
from faults.security import C2Beacon, CredentialStuffing, DnsFailure

FAULT_CLASSES: dict[str, type[Fault]] = {
    c.type: c
    for c in (
        MemoryLeak,
        DbConnExhaustion,
        DiskFull,
        LatencyInjection,
        BadConfigDeploy,
        ProcessCrash,
        CacheFailure,
        C2Beacon,
        CredentialStuffing,
        DnsFailure,
    )
}

#: Fault types the agent and scenario generator know about (dns_failure is held out).
KNOWN_FAULT_TYPES: tuple[str, ...] = tuple(t for t in FAULT_CLASSES if t != "dns_failure")
UNSEEN_FAULT_TYPES: tuple[str, ...] = ("dns_failure",)

#: Which services each fault type can target.
VALID_TARGETS: dict[str, tuple[str, ...]] = {
    "memory_leak": ("api", "worker", "gateway"),
    "db_conn_exhaustion": ("db",),
    "disk_full": ("db", "worker"),
    "latency_injection": ("api", "db", "cache"),
    "bad_config_deploy": ("gateway", "api"),
    "process_crash": ("api", "worker", "cache"),
    "cache_failure": ("cache",),
    "c2_beacon": ("worker", "api"),
    "credential_stuffing": ("gateway",),
    "dns_failure": ("api", "worker"),
}


def make_fault(spec: FaultSpec, rng: np.random.Generator) -> Fault:
    """Instantiate a fault from its spec."""
    cls = FAULT_CLASSES[spec.type]
    return cls(spec, rng)
