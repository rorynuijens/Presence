#!/usr/bin/env bash
# Turn a Presence.flatpak bundle into a signed Flatpak repository that
# GNOME Software can install from and update.
#
#   publish-repo.sh BUNDLE OUTDIR BASE_URL
#
# The signing key is read from $FLATPAK_GPG_KEY (an ASCII-armoured secret
# key).  OUTDIR receives:
#
#   repo/                 the OSTree repository, signed
#   presence.flatpakrepo  adds the repository as a remote
#   Presence.flatpakref   installs the app in one click
#   index.html            says what the two files are for
#
# Each release publishes a fresh repository holding only its own build.
# Nothing a client needs to update is lost by that: `flatpak update` pulls
# the new commit whatever came before it, and Pages stays far below its
# size limit without any pruning.

set -euo pipefail

bundle=$1
out=$2
base_url=${3%/}

app_id=io.gitlab.gtk4_apps1.Presence

: "${FLATPAK_GPG_KEY:?set FLATPAK_GPG_KEY to the armoured secret signing key}"

gnupg=$(mktemp -d)
trap 'rm -rf "$gnupg"' EXIT
chmod 700 "$gnupg"
printf '%s\n' "$FLATPAK_GPG_KEY" | gpg --homedir "$gnupg" --batch --quiet --import
key=$(gpg --homedir "$gnupg" --list-secret-keys --with-colons \
      | awk -F: '/^fpr:/ { print $10; exit }')
pubkey=$(gpg --homedir "$gnupg" --export "$key" | base64 -w0)

sign=(--gpg-sign="$key" --gpg-homedir="$gnupg")

rm -rf "$out"
mkdir -p "$out"
ostree init --mode=archive-z2 --repo="$out/repo"

flatpak build-import-bundle "${sign[@]}" "$out/repo" "$bundle"
flatpak build-update-repo "${sign[@]}" \
    --title="Presence" \
    --default-branch=master \
    --generate-static-deltas \
    "$out/repo"

cat > "$out/presence.flatpakrepo" <<EOF
[Flatpak Repo]
Title=Presence
Url=$base_url/repo/
Homepage=$base_url/
Comment=Markdown to slides
DefaultBranch=master
GPGKey=$pubkey
EOF

cat > "$out/Presence.flatpakref" <<EOF
[Flatpak Ref]
Name=$app_id
Branch=master
Title=Presence
Url=$base_url/repo/
IsRuntime=false
SuggestRemoteName=presence
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
GPGKey=$pubkey
EOF

cat > "$out/index.html" <<EOF
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Presence</title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 16px; }
  code { background: #8882; padding: 0 .3em; border-radius: 3px; }
</style>
<h1>Presence</h1>
<p><a href="Presence.flatpakref">Install Presence</a> — opens in GNOME Software,
which keeps it up to date from then on.</p>
<p>Or from a terminal:</p>
<p><code>flatpak install --user $base_url/Presence.flatpakref</code></p>
EOF
