"""Versioned rule store.

Reservation and substitution rules are immutable once published.  Replaying
a recomputation under *any* historical version is therefore just a lookup --
the engine receives the exact :class:`RuleVersion` object named by the run.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

from .errors import UnknownRuleVersionError, ValidationError
from .models import ReservationRule, RuleVersion, SubstituteEdge


class RuleStore:
    def __init__(self, versions: Sequence[RuleVersion] = ()) -> None:
        self._versions: Dict[str, RuleVersion] = {}
        for version in versions:
            self.publish(version)

    def publish(self, version: RuleVersion) -> None:
        if version.version in self._versions:
            raise ValidationError(
                f"rule version {version.version} already published and is immutable"
            )
        self._versions[version.version] = version

    def get(self, version: str) -> RuleVersion:
        try:
            return self._versions[version]
        except KeyError as exc:
            raise UnknownRuleVersionError(
                f"rule version {version!r} not found; "
                f"known versions: {sorted(self._versions)}"
            ) from exc

    def latest(self) -> RuleVersion:
        if not self._versions:
            raise UnknownRuleVersionError("rule store is empty")
        return max(
            self._versions.values(), key=lambda v: (v.created_at, v.version)
        )

    def versions(self) -> List[str]:
        return sorted(self._versions)

    @classmethod
    def from_payload(cls, payload: dict) -> "RuleStore":
        store = cls()
        for raw in payload.get("rule_versions", []):
            reservations = tuple(
                ReservationRule(
                    sku=r["sku"],
                    reservation_class=r["reservation_class"],
                    allow_relaxation=bool(r.get("allow_relaxation", False)),
                )
                for r in raw.get("reservations", [])
            )
            substitutions = tuple(
                SubstituteEdge(
                    from_sku=edge["from_sku"],
                    to_sku=edge["to_sku"],
                    rank=int(edge.get("rank", 1)),
                    allowed_classes=tuple(edge.get("allowed_classes", [])),
                )
                for edge in raw.get("substitutions", [])
            )
            store.publish(
                RuleVersion(
                    version=str(raw["version"]),
                    reservations=reservations,
                    substitutions=substitutions,
                    created_at=int(raw.get("created_at", 0)),
                )
            )
        return store
