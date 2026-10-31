"""Extensible local/cloud execution routing.

Cloud providers are configuration-driven (no hard dependency on any vendor):

    BRIDGE_CLOUD_PROVIDERS='[
      {"name":"gpu-ssh","type":"ssh","host":"gpu.example.com","user":"ubuntu",
       "key_path":"C:/Users/me/.ssh/id_ed25519","gpu":true,"cpu_cores":32,
       "memory_gb":128,"cost_per_hour_usd":1.2},
      {"name":"docker-gpu","type":"docker","image":"pytorch/pytorch:latest",
       "gpu":true,"cpus":8,"memory_gb":32,"cost_per_hour_usd":0.0}
    ]'

Supported built-in provider types: ``ssh``, ``docker`` (also podman), ``http``.
Any object implementing the ``CloudProvider`` interface can be registered at
runtime, so adding RunPod/Lambda/AWS/Kubernetes later does not change the
bridge core.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.bridge.runner import CancellationToken, clamp_timeout, resources, run_process
from app.bridge.security import redact, redact_obj

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class CloudProvider:
    """Minimal interface every provider must implement."""

    type_name = "base"

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = config or {}
        self._available: bool | None = None
        self._available_at = 0.0

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type_name,
            "configured": True,
            "gpu": _bool(self.config.get("gpu", False)),
            "cpu_cores": self.config.get("cpu_cores"),
            "memory_gb": self.config.get("memory_gb"),
            "cost_per_hour_usd": self.config.get("cost_per_hour_usd", 0.0),
            "description": self.config.get("description", ""),
        }

    def available(self, force: bool = False) -> bool:
        return True

    def capacity(self) -> dict[str, Any]:
        return {
            "cpu_cores": self.config.get("cpu_cores"),
            "memory_gb": self.config.get("memory_gb"),
            "gpu": _bool(self.config.get("gpu", False)),
            "gpu_count": int(self.config.get("gpu_count", 1 if _bool(self.config.get("gpu", False)) else 0)),
            "disk_gb": self.config.get("disk_gb"),
        }

    def estimate_cost(self, job: dict[str, Any]) -> float:
        hourly = float(self.config.get("cost_per_hour_usd", 0.0) or 0.0)
        seconds = float(job.get("timeout", job.get("timeout_seconds", 300)) or 300)
        return round(hourly * seconds / 3600.0, 6)

    def submit(self, job: dict[str, Any], token: CancellationToken | None = None) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SSH provider (uses the system OpenSSH client; no Python dependency)
# ---------------------------------------------------------------------------


class SSHProvider(CloudProvider):
    type_name = "ssh"

    @property
    def target(self) -> str:
        user = self.config.get("user", "")
        host = self.config.get("host", "")
        return f"{user}@{host}" if user else str(host)

    def _base_args(self) -> list[str]:
        args = [self.config.get("ssh_binary", "ssh"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        if self.config.get("strict_host_key_checking", True):
            args += ["-o", "StrictHostKeyChecking=accept-new"]
        if self.config.get("port"):
            args += ["-p", str(self.config["port"])]
        if self.config.get("key_path"):
            args += ["-i", str(self.config["key_path"])]
        args.append(self.target)
        return args

    def available(self, force: bool = False) -> bool:
        now = time.time()
        if not force and self._available is not None and now - self._available_at < 60:
            return self._available
        if not self.config.get("host"):
            self._available = False
        elif not shutil.which(self.config.get("ssh_binary", "ssh")):
            self._available = False
        else:
            result = run_process(self._base_args() + ["echo BRIDGE_OK"], timeout=12, shell=False, max_output=2000)
            self._available = result.ok and "BRIDGE_OK" in result.stdout
        self._available_at = now
        return bool(self._available)

    def submit(self, job: dict[str, Any], token: CancellationToken | None = None) -> dict[str, Any]:
        timeout = clamp_timeout(job.get("timeout", 300), maximum=7200)
        remote_workdir = str(job.get("remote_workdir") or self.config.get("workdir") or "~")
        files = job.get("files") or {}
        uploaded: list[str] = []
        if files:
            if not isinstance(files, dict):
                return {"ok": False, "provider": self.name, "error": "files must be an object {relative_path: content}"}
            for rel, content in files.items():
                clean = str(rel).replace("\\", "/").lstrip("/")
                if ".." in Path(clean).parts:
                    return {"ok": False, "provider": self.name, "error": f"Unsafe remote file path: {rel}"}
                dest = f"{remote_workdir}/{clean}" if remote_workdir not in ("", "~") else clean
                parent = str(Path(dest).parent).replace("\\", "/")
                payload = base64.b64encode(str(content).encode("utf-8")).decode("ascii")
                command = f"mkdir -p {shlex.quote(parent)} && printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(dest)}"
                up = run_process(self._base_args() + [command], timeout=60, shell=False, token=token)
                if not up.ok:
                    return {"ok": False, "provider": self.name, "error": f"Upload failed for {rel}: {up.error or up.stderr}"}
                uploaded.append(dest)

        if job.get("command"):
            remote_cmd = str(job["command"])
        elif job.get("code"):
            language = str(job.get("language", "python")).lower()
            encoded = base64.b64encode(str(job["code"]).encode("utf-8")).decode("ascii")
            if language in ("python", "py"):
                py = self.config.get("python", "python3")
                remote_cmd = (
                    f"{shlex.quote(py)} -c \"import base64;exec(compile(base64.b64decode('{encoded}').decode(),"
                    f"'bridge_job.py','exec'))\""
                )
            elif language in ("bash", "sh"):
                remote_cmd = f"printf %s '{encoded}' | base64 -d | bash"
            elif language in ("powershell", "pwsh"):
                remote_cmd = f"printf %s '{encoded}' | base64 -d | pwsh -NoProfile -NonInteractive -Command -"
            else:
                return {"ok": False, "provider": self.name, "error": f"Unsupported SSH job language: {language}"}
        else:
            return {"ok": False, "provider": self.name, "error": "Job needs 'command' or 'code'"}

        if remote_workdir and remote_workdir != "~":
            remote_cmd = f"cd {shlex.quote(remote_workdir)} && {remote_cmd}"
        args = self._base_args() + [remote_cmd]
        started = time.time()
        result = run_process(args, timeout=timeout, shell=False, token=token)
        out = result.as_dict()
        out.update(
            {
                "provider": self.name,
                "provider_type": "ssh",
                "target": "cloud",
                "remote_workdir": remote_workdir,
                "uploaded_files": uploaded,
                "elapsed_seconds": round(time.time() - started, 2),
            }
        )
        return out


# ---------------------------------------------------------------------------
# Container provider (Docker / Podman, optional GPU)
# ---------------------------------------------------------------------------


class ContainerProvider(CloudProvider):
    type_name = "docker"

    @property
    def runtime(self) -> str:
        return str(self.config.get("runtime") or "docker")

    def available(self, force: bool = False) -> bool:
        now = time.time()
        if not force and self._available is not None and now - self._available_at < 60:
            return self._available
        binary = shutil.which(self.runtime)
        if not binary:
            self._available = False
        else:
            result = run_process([binary, "version", "--format", "{{.Server.Version}}"], timeout=10, shell=False, max_output=2000)
            self._available = result.ok
        self._available_at = now
        return bool(self._available)

    def submit(self, job: dict[str, Any], token: CancellationToken | None = None) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "provider": self.name, "error": f"{self.runtime} is not available/running"}
        image = self.config.get("image")
        if not image:
            return {"ok": False, "provider": self.name, "error": "Container provider requires an 'image'"}
        timeout = clamp_timeout(job.get("timeout", 300), maximum=7200)
        workdir = Path(tempfile.mkdtemp(prefix="bridge_container_"))
        try:
            files = job.get("files") or {}
            if not isinstance(files, dict):
                return {"ok": False, "provider": self.name, "error": "files must be an object"}
            for rel, content in files.items():
                clean = str(rel).replace("\\", "/").lstrip("/")
                if ".." in Path(clean).parts:
                    return {"ok": False, "provider": self.name, "error": f"Unsafe file path: {rel}"}
                dest = workdir / clean
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(content), encoding="utf-8")

            if job.get("command"):
                inner = str(job["command"])
            elif job.get("code"):
                language = str(job.get("language", "python")).lower()
                ext = {"python": ".py", "py": ".py", "bash": ".sh", "sh": ".sh"}.get(language)
                if not ext:
                    return {"ok": False, "provider": self.name, "error": f"Unsupported container job language: {language}"}
                script = workdir / f"bridge_job{ext}"
                script.write_text(str(job["code"]), encoding="utf-8")
                inner = f"python bridge_job.py" if ext == ".py" else f"bash bridge_job.sh"
            else:
                return {"ok": False, "provider": self.name, "error": "Job needs 'command' or 'code'"}

            args = [self.runtime, "run", "--rm", "-v", f"{workdir}:/workspace", "-w", "/workspace"]
            if self.config.get("cpus"):
                args += ["--cpus", str(self.config["cpus"])]
            if self.config.get("memory_gb"):
                args += ["--memory", f"{self.config['memory_gb']}g"]
            if _bool(self.config.get("gpu", False)) or _bool(job.get("gpu", False)):
                args += ["--gpus", str(self.config.get("gpu_device", "all"))]
            for key, value in (self.config.get("env") or {}).items():
                args += ["-e", f"{key}={value}"]
            network = self.config.get("network", "none")
            args += ["--network", str(network), str(image), "/bin/sh", "-lc", inner]

            started = time.time()
            result = run_process(args, timeout=timeout, shell=False, token=token)
            out = result.as_dict()
            out.update(
                {
                    "provider": self.name,
                    "provider_type": "docker",
                    "target": "cloud",
                    "image": image,
                    "elapsed_seconds": round(time.time() - started, 2),
                }
            )
            return out
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Generic HTTP job provider (RunPod/Lambda/custom queue)
# ---------------------------------------------------------------------------


class HTTPProvider(CloudProvider):
    type_name = "http"

    def available(self, force: bool = False) -> bool:
        now = time.time()
        if not force and self._available is not None and now - self._available_at < 60:
            return self._available
        if not self.config.get("url"):
            self._available = False
            self._available_at = now
            return False
        health = self.config.get("health_url")
        if not health:
            self._available = True
            self._available_at = now
            return True
        try:
            import httpx

            resp = httpx.get(health, timeout=8.0, headers=self._headers())
            self._available = resp.status_code < 500
        except Exception:
            self._available = False
        self._available_at = now
        return bool(self._available)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self.config.get("token")
        token_env = self.config.get("token_env")
        if token_env:
            token = os.getenv(str(token_env), token)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers.update(self.config.get("headers") or {})
        return headers

    def submit(self, job: dict[str, Any], token: CancellationToken | None = None) -> dict[str, Any]:
        try:
            import httpx
        except Exception as exc:
            return {"ok": False, "provider": self.name, "error": f"httpx unavailable: {exc}"}
        if not self.available():
            return {"ok": False, "provider": self.name, "error": "HTTP provider health check failed"}
        timeout = clamp_timeout(job.get("timeout", 300), maximum=7200)
        payload = redact_obj({"job": job, "client": "dspy-bridge"})
        try:
            resp = httpx.post(str(self.config["url"]), json=payload, headers=self._headers(), timeout=timeout)
            body = resp.text[:100_000]
            data: dict[str, Any]
            try:
                parsed = resp.json()
                data = parsed if isinstance(parsed, dict) else {"result": parsed}
            except Exception:
                data = {"stdout": body}
            out = {
                "ok": resp.is_success and bool(data.get("ok", True)),
                "provider": self.name,
                "provider_type": "http",
                "target": "cloud",
                "status_code": resp.status_code,
                "body": redact(body),
                **data,
            }
            return out
        except Exception as exc:
            return {"ok": False, "provider": self.name, "error": str(exc)}


# ---------------------------------------------------------------------------
# Registry and planner
# ---------------------------------------------------------------------------


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, CloudProvider] = {}
        self._loaded = False
        self._lock = threading.RLock()

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            # Mark loaded before parsing so a provider that registers during
            # configuration cannot recursively call _load().
            self._loaded = True
            raw = os.getenv("BRIDGE_CLOUD_PROVIDERS", "").strip()
            if not raw:
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid BRIDGE_CLOUD_PROVIDERS JSON: %s", exc)
                return
            if isinstance(data, dict):
                items = [{"name": name, **cfg} if isinstance(cfg, dict) else {"name": name, "value": cfg} for name, cfg in data.items()]
            elif isinstance(data, list):
                items = data
            else:
                logger.warning("BRIDGE_CLOUD_PROVIDERS must be a JSON object or list")
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("type") or "provider")
                try:
                    self.register_from_config(name, item)
                except Exception as exc:
                    logger.warning("Could not configure cloud provider %s: %s", name, exc)

    def register(self, provider: CloudProvider) -> None:
        self._load()
        with self._lock:
            self._providers[provider.name] = provider

    def register_from_config(self, name: str, config: dict[str, Any]) -> CloudProvider:
        kind = str(config.get("type", "http")).lower()
        if kind == "ssh":
            provider: CloudProvider = SSHProvider(name, config)
        elif kind in ("docker", "podman", "container"):
            provider = ContainerProvider(name, {**config, "runtime": config.get("runtime", "docker" if kind != "podman" else "podman")})
        elif kind in ("http", "webhook", "lambda"):
            provider = HTTPProvider(name, config)
        else:
            raise ValueError(f"Unknown cloud provider type: {kind}")
        self._providers[name] = provider
        return provider

    def get(self, name: str) -> CloudProvider | None:
        self._load()
        return self._providers.get(name)

    def all(self) -> list[CloudProvider]:
        self._load()
        return list(self._providers.values())

    def describe(self, check_availability: bool = False) -> list[dict[str, Any]]:
        rows = []
        for provider in self.all():
            row = provider.describe()
            if check_availability:
                row["available"] = provider.available()
            rows.append(row)
        return rows

    def available(self, force: bool = False) -> list[CloudProvider]:
        return [p for p in self.all() if p.available(force=force)]


providers = ProviderRegistry()


class ExecutionPlanner:
    def __init__(self, registry: ProviderRegistry | None = None):
        self.registry = registry or providers

    def local_capacity(self, cwd: str | Path | None = None) -> dict[str, Any]:
        return resources.capacity(cwd=cwd)

    def plan(self, requirements: dict[str, Any] | None = None) -> dict[str, Any]:
        req = requirements or {}
        min_cpu = int(req.get("min_cpu", req.get("cpu_cores", 0)) or 0)
        min_mem = float(req.get("min_memory_gb", req.get("memory_gb", 0)) or 0)
        min_disk = float(req.get("min_disk_gb", 0) or 0)
        need_gpu = _bool(req.get("gpu", req.get("requires_gpu", False)))
        max_cost = req.get("max_cost_usd")
        prefer = str(req.get("prefer", req.get("target", "auto")) or "auto").lower()
        priority = str(req.get("priority", "capability") or "capability").lower()
        local = self.local_capacity(req.get("cwd"))
        local_fit = (
            local.get("cpu_cores", 0) >= min_cpu
            and float(local.get("memory_gb") or 0) >= min_mem
            and bool(local.get("disk_free_gb") is None or float(local.get("disk_free_gb") or 0) >= min_disk)
            and (not need_gpu or int(local.get("gpu_count", 0)) > 0)
        )
        candidates: list[dict[str, Any]] = []
        for provider in self.registry.all():
            cap = provider.capacity()
            fits = (
                (cap.get("cpu_cores") is None or int(cap["cpu_cores"]) >= min_cpu)
                and (cap.get("memory_gb") is None or float(cap["memory_gb"]) >= min_mem)
                and (cap.get("disk_gb") is None or float(cap["disk_gb"]) >= min_disk)
                and (not need_gpu or bool(cap.get("gpu")))
            )
            cost = provider.estimate_cost({**req, "timeout": req.get("timeout_seconds", req.get("timeout", 300))})
            if max_cost is not None and cost > float(max_cost):
                fits = False
            candidates.append(
                {
                    "name": provider.name,
                    "type": provider.type_name,
                    "fits": fits,
                    "available": provider.available(),
                    "cost_estimate_usd": cost,
                    "capacity": cap,
                }
            )

        if prefer not in ("auto", "local", "cloud"):
            chosen = next((c for c in candidates if c["name"] == prefer), None)
            if chosen and chosen["fits"] and chosen["available"]:
                return self._result("cloud", chosen["name"], local, candidates, f"Explicit provider '{prefer}' selected")
            return {
                "ok": False,
                "reason": f"Provider '{prefer}' is not configured, available, or does not meet requirements",
                "local_capacity": local,
                "candidates": candidates,
            }

        if prefer == "local" or (prefer == "auto" and local_fit and not _bool(req.get("requires_cloud", False))):
            if not local_fit:
                return {
                    "ok": False,
                    "reason": "Local machine does not meet the requested capacity and requires_cloud is false",
                    "local_capacity": local,
                    "candidates": candidates,
                }
            return self._result("local", "local", local, candidates, "Local capacity meets requirements")

        if _bool(req.get("requires_cloud")) or not local_fit or prefer == "cloud":
            usable = [c for c in candidates if c["fits"] and c["available"]]
            if not usable:
                return {
                    "ok": False,
                    "reason": "No configured cloud provider meets the requirements and is currently available",
                    "local_capacity": local,
                    "candidates": candidates,
                }
            if priority == "cost":
                usable.sort(key=lambda c: (c["cost_estimate_usd"], c["name"]))
            elif priority == "speed":
                usable.sort(key=lambda c: (-(int(c["capacity"].get("cpu_cores") or 0)), c["cost_estimate_usd"]))
            else:
                usable.sort(key=lambda c: (not c["capacity"].get("gpu"), c["cost_estimate_usd"]))
            chosen = usable[0]
            return self._result("cloud", chosen["name"], local, candidates, f"Cloud provider '{chosen['name']}' selected by {priority} priority")

        return self._result("local", "local", local, candidates, "Local execution selected")

    @staticmethod
    def _result(target: str, provider: str, local: dict[str, Any], candidates: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        return {
            "ok": True,
            "target": target,
            "provider": provider,
            "reason": reason,
            "local_capacity": local,
            "candidates": candidates,
        }


planner = ExecutionPlanner()


def cloud_providers_info(check_availability: bool = False) -> dict[str, Any]:
    rows = providers.describe(check_availability=check_availability)
    return {
        "count": len(rows),
        "providers": rows,
        "available_count": sum(1 for r in rows if r.get("available")) if check_availability else None,
    }
