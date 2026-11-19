#!/usr/bin/env bash
# Build the H16 APK from the desktop renderer and the Android host.
set -euo pipefail
cd "$(dirname "$0")/.."

sdk="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/.local/share/aivuda-android}}"
tools="$sdk/build-tools/34.0.0"
platform="$sdk/platforms/android-29/android.jar"
output="$PWD/android/build"
key="${ACESWARM_SIGNING_KEY:-${XDG_DATA_HOME:-$HOME/.local/share}/aceswarm-android/development.p12}"
for cmd in node npm javac jar keytool; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Missing required build tool: $cmd" >&2
        exit 1
    fi
done
for exe in aapt d8 zipalign apksigner; do
    if [[ ! -x "$tools/$exe" ]]; then
        echo "Install Android SDK Build Tools 34.0.0 and set ANDROID_SDK_ROOT." >&2
        exit 1
    fi
done
if [[ ! -f "$platform" ]]; then
    echo "Install Android SDK Platform 29 under $sdk." >&2
    exit 1
fi

mkdir -p "$output"
# npm ci replaces node_modules. Serialize builds from this checkout.
exec 9<"$output"
flock 9
stage=$(mktemp -d "$output/.build.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage"/{assets,classes,dex,generated} "$stage/res/drawable-nodpi"

npm ci --prefix android/web --prefer-offline --no-audit --no-fund
node android/web/build.cjs "$stage/assets"
test -s "$stage/assets/shell.html"
store_ui=aivudaAppStore/aivudaappstore/resources/ui
if [[ ! -f "$store_ui/package-lock.json" ]]; then
    git submodule update --init -- aivudaAppStore
fi
npm ci --prefix "$store_ui" --prefer-offline --no-audit --no-fund
npm ci --prefix android/store --prefer-offline --no-audit --no-fund --ignore-scripts
node android/store/build.mjs "$stage/assets"
cp electron/assets/aivuda_icon_256.png "$stage/res/drawable-nodpi/aceswarm.png"
resources=(-S "$stage/res")
if [[ -d android/station/res ]]; then
    resources+=(-S android/station/res)
fi
"$tools/aapt" package -f -M android/station/AndroidManifest.xml -I "$platform" \
    "${resources[@]}" -A "$stage/assets" -m -J "$stage/generated" -F "$stage/unsigned.apk"
mapfile -t sources < <(find "$stage/generated" android/station -name '*.java' -print | sort)
# D8 34.0.0 needs named synthetic parameters when javac 25 emits inner classes.
javac -parameters --release 8 -encoding UTF-8 -cp "$platform" -d "$stage/classes" "${sources[@]}"
jar cf "$stage/classes.jar" -C "$stage/classes" .
"$tools/d8" --min-api 25 --lib "$platform" --output "$stage/dex" "$stage/classes.jar"
(cd "$stage/dex" && "$tools/aapt" add "$stage/unsigned.apk" classes.dex)

if [[ -n "${ACESWARM_SIGNING_KEY:-}" ]]; then
    : "${ACESWARM_KEY_PASSWORD:?Set ACESWARM_KEY_PASSWORD for the signing keystore}"
else
    ACESWARM_KEY_PASSWORD=android
    if [[ ! -f "$key" ]]; then
        key_dir=$(dirname "$key")
        mkdir -p "$key_dir"
        (
            flock 8
            [[ ! -f "$key" ]] || exit 0
            umask 077
            key_temp=$(mktemp -d "$key_dir/.key.XXXXXXXX")
            trap 'rm -rf -- "$key_temp"' EXIT
            keytool -genkeypair -keystore "$key_temp/debug.p12" -storetype PKCS12 \
                -alias aceswarm -storepass android -keypass android -keyalg RSA -keysize 2048 \
                -validity 7300 -dname 'CN=ACEswarm Local Development'
            mv "$key_temp/debug.p12" "$key"
        ) 8<"$key_dir"
    fi
fi
export ACESWARM_KEY_PASSWORD
"$tools/zipalign" -f 4 "$stage/unsigned.apk" "$stage/aligned.apk"
"$tools/apksigner" sign --ks "$key" --ks-pass env:ACESWARM_KEY_PASSWORD \
    --out "$stage/signed.apk" "$stage/aligned.apk"
"$tools/apksigner" verify --min-sdk-version 25 "$stage/signed.apk"
"$tools/zipalign" -c 4 "$stage/signed.apk"
mv "$stage/signed.apk" "$output/aceswarm-h16.apk"
echo "Built $output/aceswarm-h16.apk"
