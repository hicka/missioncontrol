"""Service lifecycle and safe process-control contracts for the native host."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import inventory
import processes
import runtime


@pytest.fixture
def manager(monkeypatch):
    settings = {}
    monkeypatch.setattr(runtime.settings, 'load', lambda: settings)
    monkeypatch.setattr(runtime.settings, 'update', lambda name, value: settings.setdefault(name, {}).update(value))
    registry = runtime.Registry()
    monkeypatch.setattr(runtime, 'registry', registry)
    monkeypatch.setattr(runtime, '_tasks', {})
    monkeypatch.setattr(runtime, '_history', [])
    return registry


def test_parent_stop_cancels_dependents_and_persists_across_restart(manager):
    events = []
    async def run():
        manager.add('agents', 'Engine', '', start=lambda: events.append('start engine'), stop=lambda: events.append('stop engine'))
        manager.add('watcher', 'Watch', '', dependencies=('agents',), start=lambda: events.append('start watch'), stop=lambda: events.append('stop watch'))
        await manager.reconcile()
        await manager.action('agents', 'stop')
        assert events == ['start engine', 'start watch', 'stop watch', 'stop engine']
        assert manager.services['watcher'].state == 'blocked'
        await manager.reconcile()
        assert not manager.enabled('agents')
        await manager.action('agents', 'start')
        assert events[-2:] == ['start engine', 'start watch']
        await manager.close()
    asyncio.run(run())


def test_restart_orders_dependents_and_does_not_enable_disabled_services(manager):
    events = []
    async def run():
        for name, deps in [('engine', ()), ('watch', ('engine',)), ('disabled', ('engine',))]:
            manager.add(name, name, '', dependencies=deps, default=name != 'disabled',
                        start=lambda name=name: events.append('+' + name), stop=lambda name=name: events.append('-' + name))
        await manager.reconcile()
        await manager.action('engine', 'restart')
        assert events == ['+engine', '+watch', '-watch', '-engine', '+engine', '+watch']
        await manager.close()
    asyncio.run(run())


def test_failed_loop_is_visible_and_needs_explicit_restart(manager):
    attempts = []
    async def fail():
        attempts.append(1)
        raise RuntimeError('backend offline')
    async def run():
        manager.add('watch', 'Watch', '', run=fail)
        await manager.reconcile()
        await asyncio.sleep(0)
        await manager.reconcile()
        await manager.reconcile()
        assert manager.services['watch'].state == 'failed'
        assert len(attempts) == 1
        await manager.action('watch', 'restart')
        await asyncio.sleep(0)
        assert len(attempts) == 2
        await manager.close()
    asyncio.run(run())


def test_failed_child_process_health_stops_dependent_services(manager):
    health = {'error': ''}
    async def run():
        manager.add('listener', 'Listener', '', health=lambda: health['error'])
        manager.add('tunnel', 'Tunnel', '', dependencies=('listener',))
        await manager.reconcile()
        health['error'] = 'Listener process exited'
        await manager.reconcile()
        assert manager.services['listener'].state == 'failed'
        assert manager.services['tunnel'].state == 'blocked'
        await manager.close()
    asyncio.run(run())


def test_missing_channel_credentials_are_not_reported_running(monkeypatch):
    import channels
    class Channel:
        connected = False
        detail = 'Missing configuration'
        def __init__(self, *args): pass
        async def start(self): pass
    monkeypatch.setattr(channels, 'channel_settings', lambda: {'test': {}})
    monkeypatch.setitem(channels.CHANNEL_TYPES, 'test', Channel)
    with pytest.raises(RuntimeError, match='Missing configuration'):
        asyncio.run(channels.Manager().start_one('test'))


def test_shell_text_mentioning_the_app_is_not_an_app_process():
    assert not inventory._mission_command(['/bin/zsh', '-c', 'cat /tmp/Mission Control.app/Contents/Info.plist'])
    assert not inventory._mission_command(['/usr/bin/python3', '-c', 'print("example")', '/tmp/Mission Control.app/Contents/MacOS/MCBackend'])
    assert inventory._mission_command(['/tmp/Mission Control.app/Contents/MacOS/MissionControl'])
    assert inventory._mission_command(['/usr/bin/python3', 'server/main.py'], '/Users/person/missioncontrol')


def test_service_stop_reaps_inflight_tasks_and_registered_processes(manager, monkeypatch):
    stopped = []
    async def reap(service=None, agent=None):
        stopped.append(service)
    monkeypatch.setattr(processes, 'stop_service', reap)
    async def work():
        await asyncio.Event().wait()
    async def run():
        manager.add('watch', 'Watch', '', run=work)
        await manager.reconcile()
        assert any(task['service'] == 'watch' for task in runtime.task_snapshot())
        await manager.action('watch', 'stop')
        assert not any(task['service'] == 'watch' for task in runtime.task_snapshot())
        assert stopped == ['watch']
        assert runtime._history[0]['status'] == 'cancelled'
    asyncio.run(run())


def test_partial_start_failure_cleans_up_spawned_tasks(manager):
    cleaned = []
    async def background():
        await asyncio.Event().wait()
    def start():
        runtime.spawn(background(), 'channel')
        raise ValueError('configuration incomplete')
    async def run():
        manager.add('channel', 'Channel', '', start=start, stop=lambda: cleaned.append(True))
        await manager.reconcile()
        assert manager.services['channel'].state == 'failed'
        assert not runtime._tasks
        assert cleaned == [True]
        await manager.close()
    asyncio.run(run())


def test_data_lock_prevents_two_runtimes_and_releases_on_close(tmp_path):
    first = runtime.InstanceLock(tmp_path).acquire()
    try:
        with pytest.raises(RuntimeError, match='already owns'):
            runtime.InstanceLock(tmp_path).acquire()
    finally:
        first.release()
    runtime.InstanceLock(tmp_path).acquire().release()


def test_owner_exit_uses_parent_relationship_not_reused_pid(monkeypatch):
    monkeypatch.setenv('MISSIONCONTROL_OWNER_PID', '123')
    monkeypatch.setattr(os, 'getppid', lambda: 123)
    assert runtime.owner_alive()
    monkeypatch.setattr(os, 'getppid', lambda: 1)
    assert not runtime.owner_alive()


def test_parent_monitor_is_independent_from_service_startup(monkeypatch):
    shutdown = Mock()
    monkeypatch.setattr(runtime, 'owner_alive', lambda: False)
    monkeypatch.setattr(runtime, 'request_shutdown', shutdown)
    asyncio.run(runtime.monitor_owner())
    shutdown.assert_called_once()


@pytest.mark.parametrize('record', [None, {'pid': 999, 'created': 12, 'can_stop': False}, {'pid': 999, 'created': 15, 'can_stop': True}])
def test_stop_refuses_unknown_protected_or_reused_pid(monkeypatch, record):
    monkeypatch.setattr(inventory, 'process_inventory', lambda: [record] if record else [])
    constructor = Mock()
    monkeypatch.setattr(inventory.psutil, 'Process', constructor)
    with pytest.raises(ValueError):
        inventory.stop_process(999, 12)
    constructor.assert_not_called()


def test_stop_checks_birth_time_again_after_inventory(monkeypatch):
    monkeypatch.setattr(inventory, 'process_inventory', lambda: [{'pid': 999, 'created': 12, 'can_stop': True}])
    process = Mock(); process.create_time.return_value = 13
    monkeypatch.setattr(inventory.psutil, 'Process', lambda pid: process)
    with pytest.raises(ValueError, match='identity changed'):
        inventory.stop_process(999, 12)
    process.terminate.assert_not_called()


def test_owned_shutdown_never_stops_other_instances(monkeypatch):
    monkeypatch.setattr(inventory, 'process_inventory', lambda: [{'pid': 999, 'created': 12, 'can_stop': True, 'owned': False}])
    with pytest.raises(ValueError, match='another runtime'):
        inventory.stop_process(999, 12, owned_only=True)


def test_environment_tags_tool_owner_and_execution_without_changing_parent(monkeypatch):
    monkeypatch.delenv('MISSIONCONTROL_AGENT', raising=False)
    with processes.scope(agent='qa', execution='exec-1'):
        token = runtime.owner.set('agents')
        try:
            kwargs = {}
            processes._spawn_metadata(kwargs)
        finally:
            runtime.owner.reset(token)
    assert kwargs['env']['MISSIONCONTROL_AGENT'] == 'qa'
    assert kwargs['env']['MISSIONCONTROL_EXECUTION'] == 'exec-1'
    assert kwargs['env']['MISSIONCONTROL_SERVICE'] == 'agents'
    assert 'MISSIONCONTROL_AGENT' not in os.environ


def test_reaped_process_is_not_signalled_again(monkeypatch):
    kill = Mock(); monkeypatch.setattr(os, 'killpg', kill, raising=False)
    asyncio.run(processes.terminate(SimpleNamespace(returncode=0, pid=999)))
    kill.assert_not_called()


def test_service_stop_reaps_tagged_orphans_but_preserves_other_services(monkeypatch):
    rows = [{'pid': 90, 'created': 100, 'owned': True, 'can_stop': True, 'service': 'agents'},
            {'pid': 91, 'created': 101, 'owned': True, 'can_stop': True, 'service': 'tunnel'},
            {'pid': 92, 'created': 102, 'owned': False, 'can_stop': True, 'service': 'agents'}]
    monkeypatch.setattr(inventory, 'process_inventory', lambda: rows)
    stop = Mock(); monkeypatch.setattr(inventory, 'stop_process', stop)
    monkeypatch.setattr(processes, '_owned', {})
    asyncio.run(processes.stop_service(service='agents'))
    stop.assert_called_once_with(90, 100, owned_only=True)


def test_inventory_finds_reparented_tools_and_keeps_other_instances_separate(monkeypatch):
    class FakeProcess:
        def __init__(self, pid, parent, args, env):
            self.info = dict(pid=pid, ppid=parent, name='python', cmdline=args, create_time=100., username='tester')
            self.pid, self.env = pid, env
        def environ(self): return self.env
        def username(self): return 'tester'
        def status(self): return 'running'
        def memory_info(self): return SimpleNamespace(rss=1024)
    unrelated = FakeProcess(7, 1, ['python', 'unrelated.py'], {})
    unrelated.cwd = lambda: '/tmp'
    rows = [FakeProcess(5, 4, ['worker'], {}), FakeProcess(4, 1, ['detached'], {'MISSIONCONTROL_INSTANCE': runtime.INSTANCE, 'MISSIONCONTROL_SERVICE': 'agents'}),
            FakeProcess(6, 1, ['/missioncontrol/server/main.py'], {}), unrelated]
    monkeypatch.setattr(inventory.psutil, 'process_iter', lambda attrs: iter(rows))
    monkeypatch.setattr(inventory.psutil, 'Process', lambda: rows[0])
    monkeypatch.setattr(processes, 'registered', lambda: {})
    result = {p['pid']: p for p in inventory.process_inventory()}
    assert set(result) == {4, 5, 6}
    assert result[4]['owned'] and result[5]['owned'] and not result[6]['owned']
    assert result[4]['service'] == 'agents'
    assert all('cmdline' not in row and 'env' not in row for row in result.values())


def test_public_listener_releases_port_when_disabled(monkeypatch):
    import socket
    import main
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]; sock.close()
    monkeypatch.setattr(main.cfg, 'port', lambda name: port)
    async def run():
        for _ in range(2):
            task = asyncio.create_task(main._serve_public())
            try:
                for _ in range(100):
                    try:
                        _, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.close(); await writer.wait_closed(); break
                    except OSError:
                        await asyncio.sleep(.01)
                else:
                    pytest.fail('listener did not bind')
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(('127.0.0.1', port))
    asyncio.run(run())


def test_runtime_management_rejects_cross_origin_and_remote_requests():
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    response = client.post('/api/runtime/services/agents/stop', headers={'origin': 'https://untrusted.example'})
    assert response.status_code == 403
    remote = TestClient(main.app, client=('192.0.2.1', 5000))
    assert remote.get('/api/runtime').status_code == 403


def test_startup_port_probe_allows_time_wait_but_not_a_live_listener():
    import importlib.util
    from pathlib import Path
    import socket
    spec = importlib.util.spec_from_file_location('native_worker', Path(__file__).resolve().parents[1] / 'native_worker.py')
    worker = importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
    listener = socket.socket(); listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]; listener.listen()
    client = socket.create_connection(('127.0.0.1', port)); connection, _ = listener.accept()
    try:
        with pytest.raises(OSError):
            worker.check_port(port)
        connection.shutdown(socket.SHUT_RDWR); connection.close()
        client.close(); listener.close()
        worker.check_port(port)
    finally:
        connection.close(); client.close(); listener.close()
