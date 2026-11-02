from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).resolve().parents[1]
identity = os.environ.get('MC_SIGN_ID') or None
entitlements = str(root / 'packaging/macos/entitlements.plist')
a = Analysis(
    [str(root / 'native_worker.py')], pathex=[str(root / 'server')],
    datas=[(str(root / path), path) for path in ('web', 'assets', 'bundled_skills')]
          + [(str(root / path), '.') for path in ('agents.example.yaml', 'settings.example.yaml')],
    binaries=[], hiddenimports=collect_submodules('channels'),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['pytest', 'tkinter', 'pywebview', 'pystray'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MCBackend',
          console=True, debug=False, bootloader_ignore_signals=False, strip=False,
          upx=False, target_arch=None, codesign_identity=identity,
          entitlements_file=entitlements if identity else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MCBackend')
app = BUNDLE(coll, name='MCBackend.app', bundle_identifier='com.missioncontrol.native.runtime',
             info_plist={
                 'LSUIElement': True, 'LSMinimumSystemVersion': '13.0',
                 'CFBundleDisplayName': 'Mission Control Runtime',
                 'NSAppleEventsUsageDescription': 'Mission Control automates applications for tasks you assign to your agents.',
                 'NSMicrophoneUsageDescription': 'Mission Control uses the microphone for voice conversations.',
             })
