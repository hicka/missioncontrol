"""Watches, memory replication and subprocess ownership regression tests."""
import asyncio
import os
import sys
import time

import pytest

import db
import processes
import watch


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'runtime.db')
    monkeypatch.setattr(db, '_conn', None)
    yield
    if db._conn:
        db._conn.close()


class Orchestrator:
    agents = {'owner': {}}
    def __init__(self):
        self.events = []
    async def external_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


async def noop(_event):
    pass


def test_cancelled_watch_is_not_resurrected_by_inflight_poll(isolated, monkeypatch):
    wid = db.add_watch('owner', 'race', 'echo done', 'done', None, 20, 5, 'owner')
    async def poll(*args):
        db.update_watch(wid, status='cancelled')
        return 'done'
    monkeypatch.setattr(watch, '_run_poll', poll)
    orch = Orchestrator()
    asyncio.run(watch._tick(orch, noop, time.time()))
    assert db.get_watch(wid)['status'] == 'cancelled'
    assert not orch.events


def test_poll_errors_cannot_match_success_and_expire_with_evidence(isolated, monkeypatch):
    wid = db.add_watch('owner', 'error', 'echo done', 'done', None, 20, 1, 'owner')
    async def poll(*args):
        raise RuntimeError('HTTP 500: done')
    monkeypatch.setattr(watch, '_run_poll', poll)
    orch = Orchestrator()
    asyncio.run(watch._tick(orch, noop, time.time()))
    assert db.get_watch(wid)['status'] == 'expired'
    assert 'HTTP 500' in db.get_watch(wid)['last_out']
    assert len(orch.events) == 1


def test_overlapping_ticks_claim_one_poll_and_notify_once(isolated, monkeypatch):
    wid = db.add_watch('owner', 'once', 'echo done', 'done', None, 20, 3, 'owner')
    calls = []
    async def poll(*args):
        calls.append(1)
        await asyncio.sleep(.02)
        return 'done'
    monkeypatch.setattr(watch, '_run_poll', poll)
    orch = Orchestrator()
    async def run():
        await asyncio.gather(watch._tick(orch, noop, time.time()), watch._tick(orch, noop, time.time()))
    asyncio.run(run())
    assert len(calls) == 1 and len(orch.events) == 1
    assert db.get_watch(wid)['checks'] == 1


def test_slow_watch_does_not_block_another_watch(isolated, monkeypatch):
    # list_watches is newest first: slow is deliberately visited first.
    db.add_watch('owner', 'fast', 'fast', 'done', None, 20, 3, 'owner')
    db.add_watch('owner', 'slow', 'slow', 'done', None, 20, 3, 'owner')
    ordering = []
    async def poll(command, cwd, keys=False):
        if command == 'slow':
            await asyncio.sleep(.04)
        ordering.append(command)
        return 'done'
    monkeypatch.setattr(watch, '_run_poll', poll)
    asyncio.run(watch._tick(Orchestrator(), noop, time.time()))
    assert ordering == ['fast', 'slow']


def test_watch_emits_real_poll_start_and_persisted_outcome(isolated, monkeypatch):
    wid = db.add_watch('owner', 'trace', 'echo done', 'done', None, 20, 3, 'owner')
    events = []
    async def emit(event):
        events.append(event)
    async def poll(*args):
        assert events[0]['type'] == 'watch_poll'
        assert events[0]['watch']['checks'] == 1
        assert events[0]['phase'] == 'polling'
        assert 'poll' not in events[0]['watch']
        return 'done'
    monkeypatch.setattr(watch, '_run_poll', poll)
    asyncio.run(watch._tick(Orchestrator(), emit, time.time()))
    outcomes = [event for event in events if event.get('watch', {}).get('status') == 'done']
    assert len(outcomes) == 1
    assert outcomes[0]['watch']['id'] == wid
    assert db.get_watch(wid)['status'] == 'done'


def test_telemetry_delivery_failure_does_not_prevent_watch_execution(isolated, monkeypatch):
    wid = db.add_watch('owner', 'trace', 'echo done', 'done', None, 20, 3, 'owner')
    async def emit(event):
        if 'watch' in event:
            raise RuntimeError('display disconnected')
    async def poll(*args):
        return 'done'
    monkeypatch.setattr(watch, '_run_poll', poll)
    asyncio.run(watch._tick(Orchestrator(), emit, time.time()))
    assert db.get_watch(wid)['status'] == 'done'


def test_removed_agents_do_not_leave_infinite_active_watches(isolated):
    wid = db.add_watch('removed-agent', 'orphan', 'echo done', 'done', None, 20, 3, 'owner')
    asyncio.run(watch._tick(Orchestrator(), noop, time.time()))
    assert db.get_watch(wid)['status'] == 'cancelled'


def test_watch_runs_in_its_original_workspace(tmp_path):
    (tmp_path / 'state.txt').write_text('complete')
    cmd = 'type state.txt' if os.name == 'nt' else 'cat state.txt'
    assert asyncio.run(watch._run_poll(cmd, str(tmp_path))).strip() == 'complete'


def test_failed_process_output_is_never_accepted_as_success():
    with pytest.raises(RuntimeError, match='exit'):
        asyncio.run(watch._run_poll('echo done && exit 1'))


def test_edit_updates_memory_index_and_sync_timestamp(isolated):
    mid = db.add_memory('a', 'CI uses the old runner', guid='edit', ts=10)
    db.update_memory(mid, 'QA uses the new runner')
    assert not db.search_memories('CI')
    assert db.search_memories('QA')[0]['id'] == mid
    rows, _ = db.export_memories(20)
    assert rows[0]['guid'] == 'edit' and rows[0]['updated'] > 20


def test_fleet_applies_newer_edits_and_rejects_older_ones(isolated):
    base = {'guid': 'shared', 'ts': 10, 'author': 'a', 'kind': 'fact', 'text': 'original'}
    assert db.merge_memories([base], []) == 1
    assert db.merge_memories([{**base, 'updated': 30, 'text': 'revised QA'}], []) == 1
    assert db.merge_memories([{**base, 'updated': 20, 'text': 'stale'}], []) == 0
    assert db.search_memories('QA')[0]['text'] == 'revised QA'
    assert db.merge_memories([], [{'guid': 'shared', 'ts': 40}]) == 1
    assert db.merge_memories([{**base, 'updated': 50}], []) == 0


def test_short_and_unicode_memory_terms_are_retrievable(isolated):
    mid = db.add_memory('a', 'CI and QA report to José in Zürich')
    for term in ('CI', 'QA', 'José', 'Zürich'):
        assert any(m['id'] == mid for m in db.search_memories(term))
    assert db.add_memory('a', 'CI and QA report to José in Zürich') == mid


def test_cancelling_capture_reaps_the_process():
    async def run():
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)',
                    stdout=asyncio.subprocess.PIPE, **processes.spawn_options())
        task = asyncio.create_task(processes.capture(proc, 60))
        await asyncio.sleep(.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode is not None
    asyncio.run(run())


def test_subprocess_output_is_bounded_and_preserves_the_tail():
    async def run():
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', "print('x'*200000); print('END')",
                    stdout=asyncio.subprocess.PIPE, **processes.spawn_options())
        return await processes.capture(proc, 10, 1024)
    output = asyncio.run(run())
    assert len(output) < 1100 and output.endswith('END\n') and 'truncated' in output


def test_path_separator_and_pid_probe_are_native():
    paths = processes.environment()['PATH'].split(os.pathsep)
    assert any('.local' in p for p in paths)
    assert processes.pid_alive(os.getpid())
    assert not processes.pid_alive(-1)


def test_failed_watch_notification_is_retried_without_repolling(isolated, monkeypatch):
    wid = db.add_watch('owner', 'retry delivery', 'echo done', 'done', None, 20, 5, 'owner')
    attempts = []
    class Unreliable(Orchestrator):
        async def external_event(self, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('temporary storage failure')
            await super().external_event(*args, **kwargs)
    orch = Unreliable()
    async def run():
        await watch._tick(orch, noop, time.time())
        assert db.get_watch(wid)['status'] == 'done'
        assert not db.get_watch(wid)['notified']
        await watch._tick(orch, noop, time.time() + 30)
    asyncio.run(run())
    assert len(orch.events) == 1 and len(attempts) == 2
    assert db.get_watch(wid)['checks'] == 1
    assert db.get_watch(wid)['notified']
