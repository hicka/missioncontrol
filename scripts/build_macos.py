#!/usr/bin/env python3
"""Build the native app and a self-contained Python runtime. No user data is bundled."""
import argparse
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MINIMUM_MACOS = '14.0'
# What delivers updates. Only fetched for a build that names a feed, and checked against
# this digest, so a replaced download cannot end up inside a signed release.
SPARKLE_VERSION = '2.10.1'
SPARKLE_SHA256 = '1a856d2a9edb8e628de88121d7be04cf0e2167f8641650b78fca00b14244cf27'
MACHO_MAGIC = {b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_sparkle(build: Path) -> Path:
    folder = build / f'sparkle-{SPARKLE_VERSION}'
    if (folder / 'Sparkle.framework').exists():
        return folder
    archive = build / f'Sparkle-{SPARKLE_VERSION}.tar.xz'
    if not archive.exists() or sha256(archive) != SPARKLE_SHA256:
        url = f'https://github.com/sparkle-project/Sparkle/releases/download/{SPARKLE_VERSION}/{archive.name}'
        urllib.request.urlretrieve(url, archive)
    if sha256(archive) != SPARKLE_SHA256:
        archive.unlink()
        raise SystemExit(f'{archive.name} does not match the pinned digest. Not building with it.')
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(['tar', '-xf', str(archive), '-C', str(folder)], check=True)
    return folder


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open('rb') as stream:
        return stream.read(4) in MACHO_MAGIC


def sign(path: Path, identity: str, entitlements: Path | None = None):
    command = ['codesign', '--force', '--sign', identity]
    if identity != '-':
        command += ['--options', 'runtime', '--timestamp']
    if entitlements:
        command += ['--entitlements', str(entitlements)]
    subprocess.run([*command, str(path)], check=True, stdout=subprocess.DEVNULL)


def sign_inside_out(bundle: Path, identity: str, entitlements: Path | None):
    """Notarization wants every executable and library signed, including ones packaged as
    data such as Playwright's node, then each bundle around them, the deepest first."""
    depth = lambda path: len(path.parts)
    for path in sorted((p for p in bundle.rglob('*') if is_macho(p)), key=depth, reverse=True):
        sign(path, identity, entitlements)
    nested = [p for p in bundle.rglob('*') if p.is_dir() and not p.is_symlink() and p.suffix in ('.app', '.framework', '.xpc')]
    for path in sorted(nested, key=depth, reverse=True):
        sign(path, identity, entitlements)
    sign(bundle, identity, entitlements)


def sign_sparkle(framework: Path, identity: str):
    """In the order Sparkle documents. Downloader keeps the entitlements it shipped with."""
    current = framework / 'Versions/B'
    sign(current / 'XPCServices/Installer.xpc', identity)
    command = ['codesign', '--force', '--sign', identity, '--preserve-metadata=entitlements']
    if identity != '-':
        command += ['--options', 'runtime', '--timestamp']
    subprocess.run([*command, str(current / 'XPCServices/Downloader.xpc')], check=True, stdout=subprocess.DEVNULL)
    sign(current / 'Autoupdate', identity)
    sign(current / 'Updater.app', identity)
    sign(framework, identity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-runtime', action='store_true', help='Reuse the previously frozen backend')
    parser.add_argument('--no-runtime', action='store_true', help='Development-only: no bundled backend; the app attaches to a running server')
    parser.add_argument('--without-browser', action='store_true', help='Development-only: use an externally installed Playwright browser')
    parser.add_argument('--signing-identity', default='-', help='Code-signing identity; defaults to ad hoc local signing')
    parser.add_argument('--version', default='1.2.0')
    parser.add_argument('--debug', action='store_true', help='Compile without optimization, for a quick look at a change')
    parser.add_argument('--feed-url', help='Where releases are announced. Embeds Sparkle and turns on automatic updates')
    parser.add_argument('--public-key', help="Sparkle's public EdDSA key; updates must be signed by its private half")
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('The native macOS app must be built on macOS with Xcode command line tools.')
    if bool(args.feed_url) != bool(args.public_key):
        parser.error('--feed-url and --public-key go together: an update feed needs a key to check against.')
    build = ROOT / 'build/native'
    dist = ROOT / 'dist/native'
    build.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)
    runtime_entitlements = ROOT / 'packaging/macos/entitlements.plist'
    host_entitlements = ROOT / 'packaging/macos/host.entitlements'
    if not args.skip_runtime and not args.no_runtime:
        # Signed afterwards with everything else, so the frozen build itself stays unsigned.
        env = {key: value for key, value in os.environ.items() if key != 'MC_SIGN_ID'}
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(build / 'backend'),
                        '--workpath', str(build / 'pyinstaller'), str(ROOT / 'packaging/macos/backend.spec')],
                       cwd=ROOT, env=env, check=True)
    backend = build / 'backend/MCBackend.app'
    if not args.no_runtime and not backend.exists():
        parser.error('No frozen backend found. Build once without --skip-runtime.')
    # Assemble in a staging directory so a failed compile cannot damage the last build.
    staging = build / 'staging/Mission Control.app'
    if staging.exists():
        shutil.rmtree(staging)
    contents = staging / 'Contents'
    for folder in ('MacOS', 'Resources', 'Helpers', 'Frameworks'):
        (contents / folder).mkdir(parents=True, exist_ok=True)
    swift_files = sorted(str(path) for path in (ROOT / 'native/macos').glob('*.swift'))
    arch = os.uname().machine
    compile_command = ['xcrun', 'swiftc', '-swift-version', '5', '-target', f'{arch}-apple-macosx{MINIMUM_MACOS}',
                       '-module-cache-path', str(build / 'swift-cache'), '-module-name', 'MissionControl',
                       *(['-Onone'] if args.debug else ['-O', '-wmo']), *swift_files,
                       '-o', str(contents / 'MacOS/MissionControl'),
                       '-framework', 'AppKit', '-framework', 'SwiftUI', '-framework', 'Carbon', '-framework', 'Charts',
                       '-framework', 'Metal', '-framework', 'QuartzCore',
                       '-framework', 'ServiceManagement', '-framework', 'AVFoundation', '-framework', 'Speech']
    sparkle = None
    if args.feed_url:
        sparkle = fetch_sparkle(build)
        compile_command += ['-F', str(sparkle), '-framework', 'Sparkle', '-Xlinker', '-rpath', '-Xlinker', '@executable_path/../Frameworks']
    subprocess.run(compile_command, cwd=ROOT, check=True)
    shutil.copy2(ROOT / 'native/macos/BarAura.metal', contents / 'Resources/BarAura.metal')
    if sparkle:
        shutil.copytree(sparkle / 'Sparkle.framework', contents / 'Frameworks/Sparkle.framework', symlinks=True)
    if not args.no_runtime:
        shutil.copytree(backend, contents / 'Helpers/MCBackend.app', symlinks=True)
    if not args.without_browser and not args.no_runtime:
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
        shutil.copytree(browser_app, contents / 'Helpers/AgentBrowser.app', symlinks=True)
    shutil.copy2(ROOT / 'assets/app.icns', contents / 'Resources/app.icns')
    info = {
        'CFBundleName': 'Mission Control', 'CFBundleDisplayName': 'Mission Control',
        'CFBundleExecutable': 'MissionControl', 'CFBundleIdentifier': 'com.missioncontrol.native',
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': args.version,
        'CFBundleShortVersionString': args.version, 'CFBundleIconFile': 'app.icns',
        'LSMinimumSystemVersion': MINIMUM_MACOS, 'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication', 'NSSupportsAutomaticTermination': False,
        'NSSupportsSuddenTermination': False,
        'NSMicrophoneUsageDescription': 'Mission Control listens when you start a voice conversation with your agent.',
        'NSSpeechRecognitionUsageDescription': 'Mission Control turns what you say into text for your agent.',
        'NSAppleEventsUsageDescription': 'Mission Control automates applications for tasks you assign to your agent.',
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    }
    if args.feed_url:
        info.update({'SUFeedURL': args.feed_url, 'SUPublicEDKey': args.public_key, 'SUEnableAutomaticChecks': True,
                     'SUAutomaticallyUpdate': True, 'SUAllowsAutomaticUpdates': True, 'SUScheduledCheckInterval': 3600})
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    for helper in sorted((contents / 'Helpers').iterdir()):
        sign_inside_out(helper, args.signing_identity, runtime_entitlements)
    if sparkle:
        sign_sparkle(contents / 'Frameworks/Sparkle.framework', args.signing_identity)
    sign(staging, args.signing_identity, host_entitlements)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(staging)], check=True)
    final = dist / 'Mission Control.app'
    if final.exists():
        shutil.rmtree(final)
    shutil.move(str(staging), final)
    print(f'Built {final}')
    if args.signing_identity == '-':
        print('Locally signed build. Public distribution requires Developer ID signing and notarization.')


if __name__ == '__main__':
    main()
