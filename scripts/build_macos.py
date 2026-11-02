#!/usr/bin/env python3
"""Build the AppKit host and a self-contained Python runtime. No user data is bundled."""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-runtime', action='store_true', help='Reuse the previously frozen backend')
    parser.add_argument('--without-browser', action='store_true', help='Development-only: use an externally installed Playwright browser')
    parser.add_argument('--signing-identity', default='-', help='Code-signing identity; defaults to ad hoc local signing')
    parser.add_argument('--version', default='1.1.0')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('The native macOS app must be built on macOS with Xcode command line tools.')
    build = ROOT / 'build/native'
    dist = ROOT / 'dist/native'
    build.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if args.signing_identity != '-':
        env['MC_SIGN_ID'] = args.signing_identity
    if not args.skip_runtime:
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(build / 'backend'),
                        '--workpath', str(build / 'pyinstaller'), str(ROOT / 'packaging/macos/backend.spec')],
                       cwd=ROOT, env=env, check=True)
    backend = build / 'backend/MCBackend.app'
    if not backend.exists():
        parser.error('No frozen backend found. Build once without --skip-runtime.')
    # Assemble in a staging directory so a failed compile cannot damage the last build.
    staging = build / 'staging/Mission Control.app'
    if staging.exists():
        shutil.rmtree(staging)
    contents = staging / 'Contents'
    for folder in ('MacOS', 'Resources', 'Helpers'):
        (contents / folder).mkdir(parents=True, exist_ok=True)
    swift_files = sorted(str(path) for path in (ROOT / 'native/macos').glob('*.swift'))
    arch = os.uname().machine
    subprocess.run(['xcrun', 'swiftc', '-swift-version', '5', '-O', '-target', f'{arch}-apple-macosx13.0',
                    '-module-cache-path', str(build / 'swift-cache'), *swift_files,
                    '-o', str(contents / 'MacOS/MissionControl'),
                    '-framework', 'AppKit', '-framework', 'SwiftUI', '-framework', 'WebKit',
                    '-framework', 'ServiceManagement', '-framework', 'AVFoundation'], cwd=ROOT, check=True)
    shutil.copytree(backend, contents / 'Helpers/MCBackend.app', symlinks=True)
    if not args.without_browser:
        plan = subprocess.check_output([sys.executable, '-m', 'playwright', 'install', '--dry-run', 'chromium'], text=True)
        locations = [Path(line.split('Install location:', 1)[1].strip()) for line in plan.splitlines() if 'Install location:' in line]
        browser_location = next((path for path in locations if path.name.startswith('chromium-')), None)
        if browser_location is None:
            parser.error('Playwright did not report its Chromium installation location.')
        if not browser_location.exists():
            subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'], check=True)
        browser_app = next(browser_location.glob('*/*.app'), None)
        if not browser_app or not browser_app.exists():
            parser.error('Chromium app not found. Run python -m playwright install chromium, then rebuild.')
        destination = contents / 'Helpers/AgentBrowser.app'
        shutil.copytree(browser_app, destination, symlinks=True)
        # Chromium contains its own nested frameworks and helper applications.
        # Re-sign the copied browser, leaving the installed Playwright copy alone.
        subprocess.run(['codesign', '--force', '--deep', '--sign', args.signing_identity,
                        '--entitlements', str(ROOT / 'packaging/macos/entitlements.plist'), str(destination)], check=True)
    shutil.copy2(ROOT / 'assets/app.icns', contents / 'Resources/app.icns')
    info = {
        'CFBundleName': 'Mission Control', 'CFBundleDisplayName': 'Mission Control',
        'CFBundleExecutable': 'MissionControl', 'CFBundleIdentifier': 'com.missioncontrol.native',
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': args.version,
        'CFBundleShortVersionString': args.version, 'CFBundleIconFile': 'app.icns',
        'LSMinimumSystemVersion': '13.0', 'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication', 'NSSupportsAutomaticTermination': False,
        'NSSupportsSuddenTermination': False,
        'NSMicrophoneUsageDescription': 'Mission Control listens when you start a voice conversation with your agents.',
        'NSAppleEventsUsageDescription': 'Mission Control automates applications for tasks you assign to your agents.',
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    }
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    command = ['codesign', '--force', '--sign', args.signing_identity,
               '--entitlements', str(ROOT / 'packaging/macos/entitlements.plist')]
    if args.signing_identity != '-':
        command += ['--options', 'runtime', '--timestamp']
    subprocess.run([*command, str(staging)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(staging)], check=True)
    final = dist / 'Mission Control.app'
    if final.exists():
        shutil.rmtree(final)
    shutil.move(str(staging), final)
    print(f'Built {final}')
    print('Open it with: open "dist/native/Mission Control.app"')
    if args.signing_identity == '-':
        print('Locally signed build. Public distribution requires Developer ID signing and notarization.')


if __name__ == '__main__':
    main()
