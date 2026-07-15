"""ServiceRegistry — derives fail-closed availability from the catalog + flags + creds + pricing.

A service is only ``AVAILABLE`` (its tool may register, a roster may reference it) when it is a
priority=="now" service AND globally feature-flagged on AND (if it needs a project) not narrowed
out AND its credentials are present AND (metered ⇒ it has a pricing entry). Anything else has a
specific non-available state the UI renders, and never becomes usable. Credential presence is
checked by env-var *presence* only — a value never leaves this module.
"""

from __future__ import annotations

import os
from enum import StrEnum

from kira.services.catalog import SERVICE_CATALOG, ServiceSpec


class ServiceState(StrEnum):
    AVAILABLE = "available"
    DISABLED = "disabled"  # priority now, but the feature flag is off (or project-narrowed out)
    DEFERRED = "deferred"  # priority later/avoid — no adapter in 10B
    MISSING_CREDENTIALS = "missing_credentials"  # enabled but a required key is unset
    UNPRICED = "unpriced"  # enabled + creds, but metered/unknown with no pricing entry


class ServiceRegistry:
    """Resolves service availability. ``enabled`` is the global opt-in flag list
    (``config.services.enabled``); ``project_services`` (optional) narrows it per project —
    a service must be in BOTH; ``priced_services`` is the set of names with a pricing entry."""

    def __init__(
        self,
        *,
        enabled: list[str] | None = None,
        priced_services: frozenset[str] = frozenset(),
        project_services: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.enabled = set(enabled or [])
        self.priced_services = priced_services
        self.project_services = set(project_services) if project_services is not None else None
        self._env = env if env is not None else os.environ

    def _creds_present(self, spec: ServiceSpec) -> bool:
        return all(bool(self._env.get(var)) for var in spec.credential_env)

    def state(self, name: str) -> ServiceState:
        spec = SERVICE_CATALOG.get(name)
        if spec is None or spec.priority in ("later", "avoid"):
            return ServiceState.DEFERRED  # no adapter exists in 10B — can't be enabled
        if name not in self.enabled or (
            self.project_services is not None and name not in self.project_services
        ):
            return ServiceState.DISABLED
        if not self._creds_present(spec):
            return ServiceState.MISSING_CREDENTIALS
        if spec.pricing == "unknown" or (
            spec.pricing == "metered" and name not in self.priced_services
        ):
            return ServiceState.UNPRICED  # fail closed on unknown/absent pricing
        return ServiceState.AVAILABLE

    def is_available(self, name: str) -> bool:
        return self.state(name) is ServiceState.AVAILABLE

    def spec(self, name: str) -> ServiceSpec | None:
        return SERVICE_CATALOG.get(name)

    def availability(self) -> list[dict]:
        """A presence-only view of every catalog service for the Hub/Studio. NEVER a key
        value — only whether the required credential env vars are set."""
        out: list[dict] = []
        for name, spec in sorted(SERVICE_CATALOG.items()):
            out.append(
                {
                    "name": name,
                    "teams": list(spec.teams),
                    "kind": spec.kind,
                    "hosted": spec.hosted,
                    "priority": spec.priority,
                    "egress": spec.egress,
                    "write": spec.write,
                    "dangerous": spec.dangerous,
                    "context_policy": spec.context_policy.value,
                    "output_trust": spec.output_trust.value,
                    "stages": sorted(spec.stages),
                    "state": self.state(name).value,
                    "credentials_present": self._creds_present(spec),
                    "credential_env": list(spec.credential_env),  # names only, never values
                    "note": spec.note,
                }
            )
        return out
