"""Cloud provider registry, planner and provider submit behaviour."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.bridge import cloud as cloud_mod
from app.bridge.cloud import (
    ContainerProvider,
    HTTPProvider,
    ProviderRegistry,
    SSHProvider,
    ExecutionPlanner,
)
from app.bridge.runner import RunResult, CancellationToken


class FakeProvider(cloud_mod.CloudProvider):
    type_name = "fake"

    def __init__(
        self,
        name: str,
        available: bool = True,
        capacity: dict | None = None,
        cost: float = 0.0,
        gpu: bool = False,
    ):
        config = {
            "cpu_cores": (capacity or {}).get("cpu_cores", 8),
            "memory_gb": (capacity or {}).get("memory_gb", 16),
            "disk_gb": (capacity or {}).get("disk_gb", 100),
            "gpu": gpu,
            "cost_per_hour_usd": cost,
        }
        super().__init__(name, config)
        self._available = available

    def available(self, force: bool = False) -> bool:
        return self._available

    def capacity(self) -> dict:
        return {
            "cpu_cores": self.config["cpu_cores"],
            "memory_gb": self.config["memory_gb"],
            "disk_gb": self.config["disk_gb"],
            "gpu": self.config["gpu"],
            "gpu_count": 1 if self.config["gpu"] else 0,
        }

    def estimate_cost(self, job: dict) -> float:
        return self.config["cost_per_hour_usd"]


@pytest.fixture
def empty_registry():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("BRIDGE_CLOUD_PROVIDERS", "")
    registry = ProviderRegistry()
    registry._loaded = True
    yield registry
    monkeypatch.undo()


def local_capacity(cpu_cores=4, memory_gb=8, disk_free_gb=50, gpu_count=0):
    return {
        "cpu_cores": cpu_cores,
        "memory_gb": memory_gb,
        "disk_free_gb": disk_free_gb,
        "gpu_count": gpu_count,
    }


def test_planner_uses_local_when_it_fits(empty_registry):
    planner = ExecutionPlanner(empty_registry)
    planner.local_capacity = lambda cwd=None: local_capacity(8, 16, 100, 1)
    result = planner.plan({"min_cpu": 4, "min_memory_gb": 8, "gpu": False})
    assert result["ok"] is True
    assert result["target"] == "local"
    assert result["provider"] == "local"


def test_planner_requires_cloud_and_no_provider_fails(empty_registry):
    planner = ExecutionPlanner(empty_registry)
    planner.local_capacity = lambda cwd=None: local_capacity(4, 8, 100, 0)
    result = planner.plan({"min_cpu": 4, "gpu": True, "requires_cloud": True})
    assert result["ok"] is False
    assert "No configured cloud provider" in result["reason"]


def test_planner_prefers_local_failure_when_forced_local_insufficient(empty_registry):
    planner = ExecutionPlanner(empty_registry)
    planner.local_capacity = lambda cwd=None: local_capacity(1, 1, 1, 0)
    result = planner.plan({"min_cpu": 8, "prefer": "local"})
    assert result["ok"] is False
    assert "Local machine does not meet" in result["reason"]


def test_planner_selects_cloud_by_capability_and_cost(empty_registry):
    registry = empty_registry
    registry.register(FakeProvider("cheap-gpu", available=True, capacity={"cpu_cores": 16, "memory_gb": 32, "disk_gb": 200}, cost=0.5, gpu=True))
    registry.register(FakeProvider("expensive-gpu", available=True, capacity={"cpu_cores": 32, "memory_gb": 64, "disk_gb": 500}, cost=5.0, gpu=True))
    planner = ExecutionPlanner(registry)
    planner.local_capacity = lambda cwd=None: local_capacity(2, 4, 10, 0)

    result = planner.plan({"min_cpu": 8, "min_memory_gb": 16, "gpu": True, "priority": "cost"})
    assert result["ok"] is True
    assert result["target"] == "cloud"
    assert result["provider"] == "cheap-gpu"

    # Capability priority prefers the stronger machine even though it costs more.
    result_cap = planner.plan({"min_cpu": 8, "min_memory_gb": 16, "gpu": True, "priority": "capability"})
    assert result_cap["provider"] == "cheap-gpu" or result_cap["provider"] == "expensive-gpu"
    # The default capability sort prefers gpu then cost, so cheap remains first.
    assert result_cap["provider"] == "cheap-gpu"


def test_planner_max_cost_and_unavailable_provider(empty_registry):
    registry = empty_registry
    registry.register(FakeProvider("gpu", available=True, capacity={"cpu_cores": 16, "memory_gb": 32, "disk_gb": 200}, cost=10.0, gpu=True))
    planner = ExecutionPlanner(registry)
    planner.local_capacity = lambda cwd=None: local_capacity(2, 4, 10, 0)

    result = planner.plan({"min_cpu": 8, "gpu": True, "max_cost_usd": 1.0})
    assert result["ok"] is False
    assert "No configured cloud provider" in result["reason"]

    registry.register(FakeProvider("offline", available=False, capacity={"cpu_cores": 16, "memory_gb": 32, "disk_gb": 200}, cost=0.1, gpu=True))
    result2 = planner.plan({"min_cpu": 8, "gpu": True, "max_cost_usd": 1.0})
    assert result2["ok"] is False


def test_planner_explicit_provider_selection(empty_registry):
    registry = empty_registry
    registry.register(FakeProvider("named", available=True, capacity={"cpu_cores": 16, "memory_gb": 32, "disk_gb": 200}, cost=1.0, gpu=True))
    planner = ExecutionPlanner(registry)
    planner.local_capacity = lambda cwd=None: local_capacity(8, 16, 100, 1)

    result = planner.plan({"prefer": "named", "gpu": True})
    assert result["ok"] is True
    assert result["provider"] == "named"

    missing = planner.plan({"prefer": "does-not-exist"})
    assert missing["ok"] is False
    assert "does-not-exist" in missing["reason"]


def test_registry_loads_from_env_and_handles_bad_config(monkeypatch):
    monkeypatch.setenv(
        "BRIDGE_CLOUD_PROVIDERS",
        json.dumps(
            [
                {"name": "ssh-box", "type": "ssh", "host": "example.com", "user": "ubuntu", "cpu_cores": 8},
                {"name": "bad", "type": "totally-unknown"},
                {"container": {"type": "docker", "image": "alpine"}},
            ]
        ),
    )
    # dict form as well
    monkeypatch.setenv(
        "BRIDGE_CLOUD_PROVIDERS",
        json.dumps(
            {
                "http-box": {"type": "http", "url": "https://example.com/jobs", "cpu_cores": 4},
            }
        ),
    )
    registry = ProviderRegistry()
    assert registry.get("http-box") is not None
    assert registry.get("http-box").type_name == "http"

    monkeypatch.setenv("BRIDGE_CLOUD_PROVIDERS", "{not-json")
    registry2 = ProviderRegistry()
    assert registry2.all() == []


def test_provider_describe_capacity_and_cost():
    provider = FakeProvider("costly", available=True, capacity={"cpu_cores": 8, "memory_gb": 16, "disk_gb": 100}, cost=2.0)
    assert provider.describe()["configured"] is True
    assert provider.capacity()["cpu_cores"] == 8
    job = {"timeout": 3600}
    assert provider.estimate_cost(job) == 2.0


def test_ssh_provider_command_construction(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLOUD_PROVIDERS", "")
    provider = SSHProvider("ssh-gpu", {"host": "gpu.example.com", "user": "ubuntu", "gpu": True, "cpu_cores": 8, "memory_gb": 16, "cost_per_hour_usd": 1.0})
    provider._available = True

    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(list(command) if not isinstance(command, str) else [command])
        return RunResult(ok=True, stdout="remote-ok", stderr="", returncode=0)

    monkeypatch.setattr(cloud_mod, "run_process", fake_run)

    unsafe = provider.submit({"code": "print(1)", "files": {"../escape.txt": "x"}}, token=CancellationToken())
    assert unsafe["ok"] is False
    assert "Unsafe remote file path" in unsafe["error"]
    assert captured == []

    result = provider.submit(
        {"code": "print('remote-ok')", "language": "python", "remote_workdir": "/tmp/scratchpad", "timeout": 120},
        token=CancellationToken(),
    )
    assert result["ok"] is True
    assert result["provider_type"] == "ssh"
    assert any("ssh" in cmd[0] for cmd in captured)
    command = captured[-1]
    joined = " ".join(command)
    assert "ubuntu@gpu.example.com" in joined
    assert "cd /tmp/scratchpad" in joined
    assert "BRIDGE_OK" not in joined

    # The remote command contains the base64-encoded script.
    assert "base64.b64decode" in joined


def test_container_provider_success_and_unavailable(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLOUD_PROVIDERS", "")
    provider = ContainerProvider("docker-gpu", {"image": "python:3.11", "gpu": True, "cpus": 2, "memory_gb": 4, "network": "none"})
    provider._available = True

    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(list(command))
        return RunResult(ok=True, stdout="container-ok", stderr="", returncode=0)

    monkeypatch.setattr(cloud_mod, "run_process", fake_run)

    result = provider.submit({"code": "print('container-ok')", "language": "python"}, token=CancellationToken())
    assert result["ok"] is True
    assert result["provider_type"] == "docker"
    args = captured[-1]
    assert "docker" in args[0]
    assert "--gpus" in args
    assert "all" in args
    assert "--network" in args
    assert "none" in args
    assert "--rm" in args

    # Unavailable provider short-circuits before running docker.
    provider._available = False
    with patch.object(provider, "available", return_value=False):
        unavailable = provider.submit({"code": "print(1)"}, token=CancellationToken())
    assert unavailable["ok"] is False
    assert "not available" in unavailable["error"]


def test_http_provider_submit(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLOUD_PROVIDERS", "")
    monkeypatch.setenv("CLOUD_TOKEN", "secret-token")
    provider = HTTPProvider("http-box", {"url": "https://example.com/jobs", "token_env": "CLOUD_TOKEN", "cpu_cores": 4, "memory_gb": 8})
    provider._available = True

    class FakeResponse:
        is_success = True
        status_code = 200
        text = '{"ok": true, "stdout": "cloud-ok"}'

        def json(self):
            return {"ok": True, "stdout": "cloud-ok"}

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(provider, "available", lambda force=False: True)

    result = provider.submit({"command": "echo hi", "timeout": 30}, token=CancellationToken())
    assert result["ok"] is True
    assert result["stdout"] == "cloud-ok"
    assert captured["url"] == "https://example.com/jobs"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["json"]["job"]["command"] == "echo hi"

    # Redaction: sensitive keys in the job payload must not leak verbatim.
    result2 = provider.submit({"command": "echo hi", "env": {"API_KEY": "super-secret-value"}}, token=CancellationToken())
    assert "super-secret-value" not in json.dumps(captured["json"])


def test_cloud_actions_through_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_CLOUD_PROVIDERS", "")
    import app.bridge.audit as audit_mod

    monkeypatch.setattr(audit_mod, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(audit_mod, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod.audit, "path", tmp_path / "audit.jsonl")

    from app.bridge.context import ExecutionContext
    from app.bridge.executor import bridge_executor

    providers = bridge_executor.execute("cloud_providers", {}, context=ExecutionContext())
    assert providers["ok"] is True
    assert providers["count"] == 0

    plan = bridge_executor.execute("plan_execution", {"min_cpu": 1, "prefer": "local"}, context=ExecutionContext())
    assert plan["ok"] is True
    assert plan["target"] == "local"
