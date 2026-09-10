"""Explicit isolated health fixture, not evidence that any resident daemon is healthy."""

import json
import os
from datetime import UTC, datetime, timedelta

from poly_weather import runtime_safety as safety
from poly_weather.business_readiness import producer_progress_sample


def seed_test_chain(root, monkeypatch, *, now=None, business_samples=True):
    at = now or datetime.now(UTC)
    monkeypatch.setattr(safety, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(safety, "_process_ownership", lambda pid, command: ("matched", None))
    for name, (filename, _, _) in safety.STATUS_SPECS.items():
        path = root / "runtime" / filename
        prior = json.loads(path.read_text()) if path.exists() else {}
        samples = {}
        if business_samples:
            def sample(at, position):
                return producer_progress_sample(run_id="isolated-provider", generation="isolated-generation",
                                                positions={"fixture-archive": position}, as_of=at,
                                                pending_work=False, completed_checks=position, connection_verified=True)
            samples = {"business_sample_previous": sample(at - timedelta(seconds=1), 1),
                       "business_sample": sample(at, 2)}
        safety.atomic_json_write(path, {**prior, "state": "connected" if name == "market" else "running",
                                       **samples,
                                       "pid": os.getpid(), "heartbeat": at.isoformat(),
                                       "updated_at": at.isoformat(), "execution_enabled": False})
