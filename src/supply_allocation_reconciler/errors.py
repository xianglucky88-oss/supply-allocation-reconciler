"""Domain exceptions.

The boundary between *expected domain outcomes* and *failures* is explicit:

* allocation infeasibility is never raised -- the engine returns partial
  allocations together with unmet reason codes;
* malformed input, unknown references and unknown rule versions are
  programming/data contract failures and raise :class:`AllocationError`
  subclasses so the pipeline can emit a ``failed`` envelope.
"""


class AllocationError(ValueError):
    """Base class for every deterministic failure of the reconciler."""


class ValidationError(AllocationError):
    """The input payload violates the data contract."""


class UnknownReferenceError(ValidationError):
    """An event or commitment references an unknown entity."""


class UnknownRuleVersionError(AllocationError):
    """Replay was requested for a rule version that does not exist."""


class InterruptedRecompute(AllocationError):
    """Raised by the injected failure hook to simulate a crash.

    The write-ahead journal has already persisted every completed phase,
    so a subsequent call resumes instead of restarting.
    """


class JournalCorrupted(AllocationError):
    """A journal/checkpoint cannot be decoded or its fingerprint mismatches."""
