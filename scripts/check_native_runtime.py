#!/usr/bin/env python3
"""Exercise the frozen runtime in a disposable workspace, without model calls."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import socket
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import psutil


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', default='dist/native/Mission Control.app')
    args = parser.parse_args()
    bundle = Path(args.app).resolve()
    worker = bundle / 'Contents/Helpers/MCBackend.app/Contents/MacOS/MCBackend'
    assert worker.is_file(), 'Build the native app first'
    checks = []
    with tempfile.TemporaryDirectory(prefix='mc-native-check-') as folder:
        root = Path(folder)
        port, public_port = free_port(), free_port()
        (root / 'settings.yaml').write_text(json.dumps({
            'llm': {'provider': 'ollama', 'model': 'test', 'base_url': 'http://127.0.0.1:9'},
            'heartbeat': {'enabled': False}, 'brief': {'enabled': False}, 'github': {'enabled': False},
            'browser': {'headless': True}, 'ports': {'app': port, 'public': public_port},
        }))
        instance = uuid.uuid4().hex
        env = {**os.environ, 'MISSIONCONTROL_HOME': str(root), 'MISSIONCONTROL_PORT': str(port),
               'MISSIONCONTROL_INSTANCE': instance, 'MISSIONCONTROL_OWNER_PID': str(os.getpid())}
        browser_bundle = bundle / 'Contents/Helpers/AgentBrowser.app'
        if browser_bundle.exists():
            info = plistlib.loads((browser_bundle / 'Contents/Info.plist').read_bytes())
            env['MISSIONCONTROL_BROWSER_EXECUTABLE'] = str(browser_bundle / 'Contents/MacOS' / info['CFBundleExecutable'])
        def api(path='/api/runtime', body=None):
            request = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        def start(log):
            process = subprocess.Popen([str(worker)], env=env, stdout=log, stderr=log)
            for _ in range(150):
                assert process.poll() is None, f'Runtime exited {process.returncode}; see {root / "runtime.log"}'
                try:
                    snapshot = api()
                    assert snapshot['pid'] == process.pid and snapshot['instance'] == instance
                    return process
                except (OSError, urllib.error.URLError):
                    time.sleep(.1)
            raise AssertionError('Runtime did not become ready')
        def stop(process):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                    raise AssertionError('Runtime did not shut down gracefully')
            # Uvicorn re-emits the original SIGTERM after lifespan cleanup.
            assert process.returncode in (0, -signal.SIGTERM), f'Runtime stopped with {process.returncode}'
        runtime_process = orphan = None
        try:
            with (root / 'runtime.log').open('w') as log:
                runtime_process = start(log)
                snapshot = api()
                assert len(snapshot['services']) >= 17 and snapshot['harness']['validation'] == 'enforced'
                assert api('/api/command', {'text': '/status'})['ok']
                checks.append('frozen startup, harness and local command')
                assert api('/api/runtime/services/watcher/stop', {})['ok']
                stop(runtime_process); runtime_process = start(log)
                watch = next(s for s in api()['services'] if s['id'] == 'watcher')
                assert not watch['enabled'] and watch['status'] == 'stopped'
                checks.append('immediate restart and persisted service state')
                assert api('/api/runtime/services/public_listener/start', {})['ok']
                for _ in range(50):
                    with socket.socket() as probe:
                        if probe.connect_ex(('127.0.0.1', public_port)) == 0: break
                    time.sleep(.1)
                else: raise AssertionError('Public listener did not start')
                assert api('/api/runtime/services/public_listener/stop', {})['ok']
                with socket.socket() as probe:
                    assert probe.connect_ex(('127.0.0.1', public_port)) != 0
                checks.append('listener control and port release')
                orphan = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'],
                    env={**env, 'MISSIONCONTROL_SERVICE': 'agents', 'MISSIONCONTROL_AGENT': 'qa'}, start_new_session=True)
                row = next(p for p in api()['processes'] if p['pid'] == orphan.pid)
                assert row['owned'] and row['service'] == 'agents'
                assert api('/api/runtime/services/agents/stop', {})['ok']
                orphan.wait(timeout=6)
                checks.append('detached tool inventory and service cleanup')
                stop(runtime_process); runtime_process = None
                result = json.loads(subprocess.check_output([str(worker), '--inventory'], env=env, timeout=20))
                assert not any(p['instance'] == instance for p in result['processes'])
                checks.append('graceful shutdown with no owned survivors')
                if browser_bundle.exists():
                    result = json.loads(subprocess.check_output([str(worker), '--check-browser'], env=env, stderr=log, timeout=30))
                    assert result['ok']
                    checks.append('bundled Chromium, offline page and cleanup')
                # A stand-in native parent crashes without signalling its child.
                # The runtime must detect reparenting and shut itself down.
                pidfile = root / 'orphan.pid'
                host_code = ('import os,subprocess,sys,time; '
                             'env=dict(os.environ, MISSIONCONTROL_OWNER_PID=str(os.getpid())); '
                             'child=subprocess.Popen([sys.argv[1]], env=env); '
                             'open(sys.argv[2],"w").write(str(child.pid)); time.sleep(60)')
                host = subprocess.Popen([sys.executable, '-c', host_code, str(worker), str(pidfile)],
                                        env=env, stdout=log, stderr=log)
                backend_pid = None
                try:
                    for _ in range(150):
                        if pidfile.exists() and pidfile.read_text():
                            backend_pid = int(pidfile.read_text())
                            try:
                                snapshot = api()
                                if snapshot['pid'] == backend_pid and snapshot['owner_pid'] == host.pid: break
                            except OSError: pass
                        time.sleep(.1)
                    else: raise AssertionError('Parent-monitor fixture did not become ready')
                    host.kill(); host.wait()
                    for _ in range(100):
                        if not psutil.pid_exists(backend_pid): break
                        if psutil.Process(backend_pid).status() == psutil.STATUS_ZOMBIE: break
                        time.sleep(.1)
                    else: raise AssertionError('Runtime survived the crashed native host')
                    checks.append('native host crash triggers runtime shutdown')
                finally:
                    if host.poll() is None: host.kill(); host.wait()
                    subprocess.run([str(worker), '--cleanup-owned'], env=env, stdout=log, stderr=log, timeout=20, check=True)
        except Exception:
            print((root / 'runtime.log').read_text()[-6000:], file=sys.stderr)
            raise
        finally:
            if orphan and orphan.poll() is None: orphan.kill(); orphan.wait()
            if runtime_process and runtime_process.poll() is None: stop(runtime_process)
    print(json.dumps({'passed': True, 'checks': checks}))


if __name__ == '__main__':
    main()
