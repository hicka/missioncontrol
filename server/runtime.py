"""One inventory and lifecycle for every Mission Control background service."""
import asyncio
import contextvars
import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

import settings

log = logging.getLogger("missioncontrol.runtime")
owner = contextvars.ContextVar("missioncontrol_service", default="runtime")
_tasks = {}
_history = []


def spawn(coroutine, service, label=None, agent=None):
    """Track work before it can execute; children inherit their service owner."""
    token = owner.set(service)
    try:
        task = asyncio.create_task(coroutine, name=f"mc:{service}:{label or service}")
    finally:
        owner.reset(token)
    record = {"id": uuid.uuid4().hex[:12], "service": service, "label": label or service,
              "agent": agent, "started": time.time(), "status": "running"}
    _tasks[task] = record
    def finished(done):
        row = _tasks.pop(done, record)
        row = {**row, "finished": time.time(), "status": "cancelled" if done.cancelled() else "completed"}
        if not done.cancelled() and done.exception():
            row["status"] = "failed"
            row["error"] = type(done.exception()).__name__
        _history.insert(0, row)
        del _history[60:]
    task.add_done_callback(finished)
    return task


async def cancel_service_tasks(service):
    current = asyncio.current_task()
    targets = [task for task, row in list(_tasks.items()) if row["service"] == service and task is not current]
    for task in targets:
        task.cancel()
    await asyncio.gather(*targets, return_exceptions=True)


def task_snapshot():
    rows = [dict(row) for row in _tasks.values()]
    # Network/library tasks also appear, rather than disappearing from inventory.
    for task in asyncio.all_tasks():
        if task in _tasks or task.done():
            continue
        coroutine = task.get_coro()
        rows.append({"id": f"io-{id(task):x}", "service": "runtime_io",
                     "label": getattr(coroutine, "__qualname__", task.get_name()),
                     "status": "running", "started": None, "managed_by": "Runtime transport"})
    return rows


@dataclass
class Service:
    id: str
    name: str
    description: str
    run: object = None
    start: object = None
    stop: object = None
    default: object = True
    configure: object = None
    health: object = None
    dependencies: tuple = ()
    settings_tab: str = ""
    group: str = "services"
    task: object = None
    state: str = "stopped"
    started: float | None = None
    error: str = ""
    restarts: int = 0
    # Comes back by itself when it fails, with a growing wait. For what nobody should
    # have to notice has gone: a dead tunnel silently loses every webhook delivery.
    auto_restart: bool = False
    retry_at: float = 0.0
    failures_in_a_row: int = 0

    def enabled(self):
        override = (settings.load().get("runtime_services") or {}).get(self.id)
        return bool(override if isinstance(override, bool) else self.default() if callable(self.default) else self.default)


class Registry:
    def __init__(self):
        self.services = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.emit = None

    def add(self, id, name, description, **kwargs):
        if id in self.services:
            raise ValueError(f"Duplicate runtime service: {id}")
        self.services[id] = Service(id, name, description, **kwargs)

    def enabled(self, id):
        return self.services[id].enabled() if id in self.services else True

    async def _start(self, service):
        service.state, service.error, service.started = "starting", "", time.time()
        token = owner.set(service.id)
        try:
            if service.start:
                result = service.start()
                if inspect.isawaitable(result):
                    await result
            if service.run:
                service.task = spawn(service.run(), service.id, service.name)
            service.state = "running"
        except Exception as exc:
            await cancel_service_tasks(service.id)
            import processes
            await processes.stop_service(service=service.id)
            if service.stop:
                try:
                    cleanup = service.stop()
                    if inspect.isawaitable(cleanup):
                        await cleanup
                except Exception:
                    log.exception("cleanup after failed service startup: %s", service.id)
            service.state, service.error = "failed", type(exc).__name__ + ": " + str(exc)[:240]
            log.exception("service %s failed to start", service.id)
        finally:
            owner.reset(token)

    async def _stop(self, service):
        service.state = "stopping"
        try:
            await cancel_service_tasks(service.id)
            if service.stop:
                result = service.stop()
                if inspect.isawaitable(result):
                    await result
            import processes
            await processes.stop_service(service=service.id)
        except Exception as exc:
            service.error = type(exc).__name__ + ": " + str(exc)[:240]
            log.exception("service %s failed to stop cleanly", service.id)
        finally:
            service.task, service.state = None, "stopped"

    async def reconcile(self):
        async with self.lock:
            await self._reconcile()

    async def _reconcile(self):
        if self.closed:
            return
        wanted = {}
        for service in self.services.values():
            wanted[service.id] = service.enabled() and all(wanted.get(dep, False) for dep in service.dependencies)
            if service.task and service.task.done() and service.state == "running":
                service.state = "failed"
                exc = None if service.task.cancelled() else service.task.exception()
                service.error = f"{type(exc).__name__}: {str(exc)[:240]}" if exc else "Service stopped unexpectedly; restart to resume."
            if service.state == "running" and service.health:
                error = service.health()
                if error:
                    service.state, service.error = "failed", str(error)[:240]
            if service.state == "failed" and service.auto_restart and wanted[service.id]:
                now = time.time()
                if not service.retry_at:
                    # Up for a good while before this means it is not the same failure again.
                    if service.started and now - service.started > 600:
                        service.failures_in_a_row = 0
                    service.failures_in_a_row += 1
                    service.retry_at = now + min(600, 15 * 2 ** service.failures_in_a_row)
                elif now >= service.retry_at:
                    log.warning("restarting %s after: %s", service.id, service.error)
                    service.retry_at = 0.0
                    service.restarts += 1
                    await self._stop(service)
        for service in reversed(list(self.services.values())):
            if not wanted[service.id] and service.state in ("running", "starting", "failed"):
                await self._stop(service)
            if not service.enabled() and service.state == "blocked":
                service.state = "stopped"
        for service in self.services.values():
            ready = all(self.services[dep].state == "running" for dep in service.dependencies)
            if wanted[service.id] and ready and service.state in ("stopped", "blocked"):
                await self._start(service)
            elif service.enabled() and not ready:
                if service.state == "running":
                    await self._stop(service)
                service.state = "blocked"

    async def action(self, id, action):
        if id not in self.services:
            raise ValueError("Unknown runtime service")
        if action not in ("start", "stop", "restart"):
            raise ValueError("Use start, stop, or restart")
        async with self.lock:
            service = self.services[id]
            enabled = action != "stop"
            if service.configure:
                service.configure(enabled)
            settings.update("runtime_services", {id: enabled})
            if action == "restart":
                # Dependents stop before their parent, then reconcile in order.
                affected = {id}
                for child in self.services.values():
                    if any(dep in affected for dep in child.dependencies):
                        affected.add(child.id)
                for child in reversed(list(self.services.values())):
                    if child.id in affected and child.state != "stopped":
                        await self._stop(child)
                service.restarts += 1
            if enabled and service.state == "failed":
                service.state = "stopped"
            await self._reconcile()
        if self.emit:
            await self.emit({"type": "runtime_updated"})

    async def close(self):
        async with self.lock:
            self.closed = True
            for service in reversed(list(self.services.values())):
                if service.state != "stopped":
                    await self._stop(service)

    def snapshot(self):
        from harness import redact
        return [{"id": s.id, "name": s.name, "description": s.description,
                 "status": s.state, "enabled": s.enabled(), "started": s.started,
                 "error": redact(s.error), "dependencies": list(s.dependencies),
                 "settings_tab": s.settings_tab, "group": s.group, "restarts": s.restarts,
                 "tasks": sum(row["service"] == s.id for row in _tasks.values())}
                for s in self.services.values()]


registry = Registry()
STARTED = time.time()
INSTANCE = os.environ.get("MISSIONCONTROL_INSTANCE") or uuid.uuid4().hex
os.environ["MISSIONCONTROL_INSTANCE"] = INSTANCE


def owner_alive():
    """A child must still be attached to the exact host that started it."""
    parent = int(os.environ.get("MISSIONCONTROL_OWNER_PID") or 0)
    if not parent:
        return True
    # Reparenting detects owner exit even if the OS quickly reuses its PID.
    return os.getppid() == parent


async def supervise():
    # Nothing watches the supervisor. One bad pass (settings.yaml caught mid-edit and not
    # parsing) used to end it for good, and with it every restart of everything else.
    while True:
        try:
            await registry.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("service supervisor pass failed")
        await asyncio.sleep(2)


async def monitor_owner():
    # Keep this independent from service startup/network requests. A stalled
    # integration must not prevent detection of a crashed native host.
    while True:
        if not owner_alive():
            request_shutdown()
            return
        await asyncio.sleep(1)


def request_shutdown():
    import signal
    os.kill(os.getpid(), signal.SIGTERM)


class InstanceLock:
    """An OS-released lock protects one data directory from duplicate runtimes."""
    def __init__(self, root):
        self.path = root / "runtime.lock"
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write("0"); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.file.close(); self.file = None
            raise RuntimeError("A Mission Control runtime already owns this data folder. Manage it in Runtime Manager.") from None
        return self

    def release(self):
        if self.file:
            self.file.close(); self.file = None
