"""Provider-neutral chat context contracts.

A1 intentionally keeps this contract small.  Later parity stages may add new
fields one at a time, but providers must not grow private copies here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SharedContext:
    persona: str
    relationship_context: str
    relationship_fingerprint: str
