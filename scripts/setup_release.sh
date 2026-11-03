#!/bin/bash
# Puts what a release needs on the repository's `release` environment: the Developer ID
# certificate, the notarization key and Sparkle's update key. Run it once, on the Mac that
# holds the certificate. Each secret goes from here straight to GitHub and nothing is written
# to the checkout. Parts already set are left alone, so it is safe to run again.
#
#   scripts/setup_release.sh --notary-key ~/Downloads/AuthKey_ABC123DEF4.p8 --issuer <issuer id>
#
# Make the notary key at App Store Connect > Users and Access > Integrations > Team Keys, with
# the Developer role. The issuer id is shown above the list of keys.
set -euo pipefail
cd "$(dirname "$0")/.."

notary_key="" issuer="" identity="${MC_SIGNING_IDENTITY:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --notary-key) notary_key="$2"; shift 2 ;;
    --issuer) issuer="$2"; shift 2 ;;
    --identity) identity="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

gh auth status >/dev/null
repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
work="$(mktemp -d)"
chmod 700 "$work"
trap 'rm -rf "$work"' EXIT

has_secret() { gh secret list --env release --repo "$repo" --json name --jq '.[].name' | grep -qxF "$1"; }
# Always from a file, so a secret never appears in a process list.
secret() { gh secret set "$1" --env release --repo "$repo" < "$2"; }
variable() { gh variable set "$1" --env release --repo "$repo" --body "$2"; }

echo "1. The release environment on $repo, open to v* tags only"
printf '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' |
  gh api -X PUT "repos/$repo/environments/release" --input - >/dev/null
if ! gh api "repos/$repo/environments/release/deployment-branch-policies" --jq '.branch_policies[].name' | grep -qxF 'v*'; then
  gh api -X POST "repos/$repo/environments/release/deployment-branch-policies" -f name='v*' -f type=tag >/dev/null
fi

echo "2. The Developer ID certificate"
if [ -z "$identity" ]; then
  identity="$(security find-identity -v -p codesigning | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)"
fi
if [ -z "$identity" ]; then
  echo "   No Developer ID Application certificate in the keychain." >&2
  exit 1
fi
variable MACOS_SIGNING_IDENTITY "$identity"
if has_secret MACOS_CERTIFICATE; then
  echo "   already set"
else
  cat > "$work/export.swift" <<'SWIFT'
import Foundation
import Security
// Exports one signing identity, found by its certificate's name, as a password-protected .p12.
let args = CommandLine.arguments
let password = try String(contentsOfFile: args[2], encoding: .utf8) as CFString
let query: [String: Any] = [kSecClass as String: kSecClassIdentity, kSecMatchLimit as String: kSecMatchLimitAll, kSecReturnRef as String: true]
var found: CFTypeRef?
guard SecItemCopyMatching(query as CFDictionary, &found) == errSecSuccess, let identities = found as? [SecIdentity] else {
    fputs("No signing identities in the keychain.\n", stderr); exit(1)
}
let identity = identities.first { identity in
    var certificate: SecCertificate?
    SecIdentityCopyCertificate(identity, &certificate)
    return certificate.flatMap { SecCertificateCopySubjectSummary($0) as String? } == args[1]
}
guard let identity else { fputs("No identity named \(args[1]).\n", stderr); exit(1) }
var parameters = SecItemImportExportKeyParameters()
parameters.version = UInt32(SEC_KEY_IMPORT_EXPORT_PARAMS_VERSION)
parameters.passphrase = Unmanaged.passUnretained(password as CFTypeRef)
var data: CFData?
let status = withExtendedLifetime(password) { SecItemExport(identity, .formatPKCS12, [], &parameters, &data) }
guard status == errSecSuccess, let data else {
    fputs("Export failed: \(SecCopyErrorMessageString(status, nil) as String? ?? "error \(status)")\n", stderr); exit(1)
}
try (data as Data).write(to: URL(fileURLWithPath: args[3]))
SWIFT
  openssl rand -base64 24 | tr -d '\n' > "$work/password"
  echo "   macOS asks you to allow exporting the key. Only this one identity leaves the keychain."
  xcrun swift "$work/export.swift" "$identity" "$work/password" "$work/certificate.p12"
  base64 -i "$work/certificate.p12" > "$work/certificate.b64"
  secret MACOS_CERTIFICATE "$work/certificate.b64"
  secret MACOS_CERTIFICATE_PASSWORD "$work/password"
fi

echo "3. The notarization key"
if has_secret NOTARY_KEY; then
  echo "   already set"
elif [ -z "$notary_key" ]; then
  echo "   skipped: pass --notary-key AuthKey_<id>.p8 and --issuer <issuer id>"
else
  if [ -z "$issuer" ]; then echo "   --issuer goes with --notary-key." >&2; exit 2; fi
  key_id="$(basename "$notary_key" .p8)"
  key_id="${key_id#AuthKey_}"
  # Proves the key works before a release depends on it.
  xcrun notarytool history --key "$notary_key" --key-id "$key_id" --issuer "$issuer" >/dev/null
  secret NOTARY_KEY "$notary_key"
  variable NOTARY_KEY_ID "$key_id"
  variable NOTARY_ISSUER "$issuer"
fi

echo "4. Sparkle's update key"
if has_secret SPARKLE_PRIVATE_KEY; then
  echo "   already set"
else
  # The same pinned download a release build uses.
  sparkle="$(python3 -c 'import sys; sys.path.insert(0, "scripts"); from pathlib import Path; import build_macos; print(build_macos.fetch_sparkle(Path("build/native")).resolve())')"
  # Makes the key in your login keychain, or uses the one already there.
  "$sparkle/bin/generate_keys" >/dev/null
  "$sparkle/bin/generate_keys" -x "$work/sparkle.key"
  secret SPARKLE_PRIVATE_KEY "$work/sparkle.key"
  variable SPARKLE_PUBLIC_KEY "$("$sparkle/bin/generate_keys" -p)"
  echo "   The private key stays in your login keychain under https://sparkle-project.org. Back it up:"
  echo "   losing it strands every installed copy on its last version."
fi

echo
echo "On the release environment now:"
gh secret list --env release --repo "$repo"
gh variable list --env release --repo "$repo"
