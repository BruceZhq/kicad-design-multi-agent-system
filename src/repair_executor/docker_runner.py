"""Docker API adapter with fixed mounts, command, user and resource policy.

Only this broker has Docker access. Model scripts run in fresh, secret-free,
network-disabled containers without host mounts or access to the Docker socket.
"""

from __future__ import annotations

import base64
import io
import tarfile
import time
import uuid
from typing import Any

import httpx

from ratsnestpro.repair.contracts import SandboxRequest, SandboxResult

_MAX_OUTPUT_BYTES = 24_000_000
_BOOTSTRAP = """
import os, resource, runpy
resource.setrlimit(resource.RLIMIT_FSIZE,(24_000_000,24_000_000))
resource.setrlimit(resource.RLIMIT_NOFILE,(256,256))
os.chdir('/work')
runpy.run_path('/work/repair.py',run_name='__main__')
"""


def container_config(image: str) -> dict[str, Any]:
    return {
        "Image": image,
        "User": "10001:10001",
        "WorkingDir": "/work",
        "Entrypoint": ["/usr/bin/python3", "-c", "import time; time.sleep(180)"],
        "Cmd": [],
        "Env": ["HOME=/tmp", "PYTHONPATH=/app", "PYTHONDONTWRITEBYTECODE=1"],
        "NetworkDisabled": True,
        "HostConfig": {
            "NetworkMode": "none",
            "CapDrop": ["ALL"],
            "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges:true"],
            "Memory": 2_147_483_648,
            "MemorySwap": 2_147_483_648,
            "NanoCpus": 1_000_000_000,
            "PidsLimit": 128,
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,size=128m",
                "/work": "rw,noexec,nosuid,size=256m,uid=10001,gid=10001,mode=0700",
            },
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
            "Binds": [],
        },
        "Labels": {"ratsnest.component": "isolated-cad-repair"},
    }


def input_archive(request: SandboxRequest) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directory = tarfile.TarInfo(".")
        directory.type, directory.mode = tarfile.DIRTYPE, 0o700
        directory.uid = directory.gid = 10001
        archive.addfile(directory)
        files = {f.path: base64.b64decode(f.data, validate=True) for f in request.files}
        files["repair.py"] = request.script.encode("utf-8")
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), 0o600
            info.uid = info.gid = 10001
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def run(
    request: SandboxRequest, *, image: str, socket_path: str = "/var/run/docker.sock"
) -> SandboxResult:
    # Deployment, never a model request, chooses the image and daemon.
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=socket_path), base_url="http://docker/v1.47", timeout=20
    ) as docker:
        volume = "ratsnest-repair-" + uuid.uuid4().hex
        created_volume = docker.post(
            "/volumes/create",
            json={
                "Name": volume,
                "Driver": "local",
                "DriverOpts": {
                    "type": "tmpfs",
                    "device": "tmpfs",
                    "o": "size=256m,uid=10001,gid=10001,mode=0700",
                },
                "Labels": {"ratsnest.component": "isolated-cad-repair"},
            },
        )
        created_volume.raise_for_status()
        identifier = seed_id = None
        try:
            config = container_config(image)
            del config["HostConfig"]["Tmpfs"]["/work"]
            config["HostConfig"]["Mounts"] = [
                {"Type": "volume", "Source": volume, "Target": "/work"}
            ]
            # Docker's archive API rejects readonly-root containers even when
            # /work is writable. A trusted seed container only sleeps; it never
            # executes model code and keeps tmpfs mounted until job handoff.
            config["HostConfig"]["ReadonlyRootfs"] = False
            seeded = docker.post("/containers/create", json=config)
            seeded.raise_for_status()
            seed_id = seeded.json()["Id"]
            docker.post(f"/containers/{seed_id}/start").raise_for_status()
            uploaded = docker.put(
                f"/containers/{seed_id}/archive",
                params={"path": "/work"},
                content=input_archive(request),
                headers={"Content-Type": "application/x-tar"},
            )
            uploaded.raise_for_status()
            config["HostConfig"]["ReadonlyRootfs"] = True
            created = docker.post("/containers/create", json=config)
            created.raise_for_status()
            identifier = created.json()["Id"]
            docker.post(f"/containers/{identifier}/start").raise_for_status()
            docker.delete(f"/containers/{seed_id}", params={"force": 1, "v": 1}).raise_for_status()
            seed_id = None
            execution = docker.post(
                f"/containers/{identifier}/exec",
                json={
                    "AttachStdout": True,
                    "AttachStderr": True,
                    "Tty": True,
                    "User": "10001:10001",
                    "WorkingDir": "/work",
                    "Cmd": ["/usr/bin/python3", "-c", _BOOTSTRAP],
                },
            )
            execution.raise_for_status()
            exec_id = execution.json()["Id"]
            output_bytes = bytearray()
            deadline = time.monotonic() + request.timeout_seconds
            try:
                with docker.stream(
                    "POST",
                    f"/exec/{exec_id}/start",
                    json={"Detach": False, "Tty": True},
                    timeout=request.timeout_seconds,
                ) as stream:
                    stream.raise_for_status()
                    for chunk in stream.iter_bytes():
                        if time.monotonic() > deadline:
                            raise httpx.ReadTimeout("script wall clock exceeded")
                        output_bytes.extend(chunk)
                        del output_bytes[:-16000]
            except httpx.TimeoutException:
                docker.post(f"/containers/{identifier}/kill")
                return SandboxResult(status="timeout")
            inspected = docker.get(f"/exec/{exec_id}/json")
            inspected.raise_for_status()
            code = int(inspected.json()["ExitCode"])
            output = output_bytes.decode("utf-8", errors="replace")
            if code:
                return SandboxResult(status="failed", exit_code=code, output=output)
            data = bytearray()
            with docker.stream(
                "GET",
                f"/containers/{identifier}/archive",
                params={"path": "/work/" + request.pcb_name},
            ) as download:
                if download.status_code == 404:
                    return SandboxResult(
                        status="failed",
                        exit_code=code,
                        output=output + "\nScript did not leave the required PCB file.",
                    )
                download.raise_for_status()
                for chunk in download.iter_bytes():
                    data.extend(chunk)
                    if len(data) > _MAX_OUTPUT_BYTES + 65536:
                        raise ValueError("candidate PCB exceeds output size limit")
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                members = archive.getmembers()
                if (
                    len(members) != 1
                    or not members[0].isfile()
                    or members[0].size > _MAX_OUTPUT_BYTES
                ):
                    raise ValueError("executor must return one regular PCB, never a symlink")
                stream = archive.extractfile(members[0])
                assert stream is not None
                pcb = stream.read(_MAX_OUTPUT_BYTES + 1)
            return SandboxResult(
                status="completed",
                exit_code=0,
                output=output,
                pcb_data=base64.b64encode(pcb).decode("ascii"),
            )
        finally:
            for owned in (identifier, seed_id):
                if owned:
                    docker.delete(f"/containers/{owned}", params={"force": 1, "v": 1})
            docker.delete(f"/volumes/{volume}")
