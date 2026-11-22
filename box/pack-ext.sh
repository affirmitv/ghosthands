#!/usr/bin/env bash
# Build step (Dockerfile): pack box/ext as a signed CRX with a key made here, and install it the
# way an organisation does: Chrome policy force-installs it from a local update manifest.
# (Branded Chrome no longer honours --load-extension.) Writes, matching the key's extension id:
#   /opt/ghostbox/ext.crx, /opt/ghostbox/ext-update.xml,
#   /etc/opt/chrome/policies/managed/ghostbox-ext.json,
#   /etc/opt/chrome/native-messaging-hosts/dev.ghostbox.vault.json (allowed_origins)
set -euo pipefail
work=$(mktemp -d)
cp -r /opt/ghostbox/ext "$work/ext"
# The id comes from the packing key; a "key" in the manifest would contradict it.
python3 - "$work/ext/manifest.json" <<'EOF'
import json, sys
p = sys.argv[1]; m = json.load(open(p)); m.pop("key", None); json.dump(m, open(p, "w"), indent=1)
EOF
openssl genrsa -out "$work/key.pem" 2048 2>/dev/null
google-chrome-stable --no-sandbox --headless=new --user-data-dir="$work/profile" \
  --pack-extension="$work/ext" --pack-extension-key="$work/key.pem" >/dev/null 2>&1 || true
[ -s "$work/ext.crx" ] || { echo "pack-ext: Chrome did not produce a CRX" >&2; exit 1; }
install -m 644 "$work/ext.crx" /opt/ghostbox/ext.crx
ID=$(openssl rsa -in "$work/key.pem" -pubout -outform DER 2>/dev/null | sha256sum | cut -c1-32 | tr '0-9a-f' 'a-p')
VER=$(python3 -c "import json;print(json.load(open('$work/ext/manifest.json'))['version'])")
cat > /opt/ghostbox/ext-update.xml <<EOF
<?xml version='1.0' encoding='UTF-8'?>
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='$ID'><updatecheck codebase='file:///opt/ghostbox/ext.crx' version='$VER' /></app>
</gupdate>
EOF
mkdir -p /etc/opt/chrome/policies/managed /etc/opt/chrome/native-messaging-hosts
cat > /etc/opt/chrome/policies/managed/ghostbox-ext.json <<EOF
{"ExtensionSettings": {"$ID": {"installation_mode": "force_installed",
  "update_url": "file:///opt/ghostbox/ext-update.xml"}}}
EOF
python3 - "$ID" <<'EOF'
import json, sys
p = "/etc/opt/chrome/native-messaging-hosts/dev.ghostbox.vault.json"
m = json.load(open(p)); m["allowed_origins"] = ["chrome-extension://%s/" % sys.argv[1]]
json.dump(m, open(p, "w"), indent=1)
EOF
echo "pack-ext: extension $ID version $VER"
