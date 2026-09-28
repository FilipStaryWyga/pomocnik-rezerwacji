#!/bin/sh
# Buduje aplikację „Pomocnik rezerwacji.app” (wersja testowa na Maca) i paczkę .zip do wysłania.
# Użycie: sh zbuduj_mac.sh
set -e
cd "$(dirname "$0")"

APP="dist/Pomocnik rezerwacji.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp pomocnik.py pomocnik.user.js "$APP/Contents/Resources/"

# Ikona
python3 ikona.py >/dev/null
IS=dist/ikona.iconset; rm -rf "$IS"; mkdir -p "$IS"
for s in 16 32 128 256 512; do
  sips -z $s $s ikona.png --out "$IS/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2)); sips -z $d $d ikona.png --out "$IS/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$IS" -o "$APP/Contents/Resources/ikona.icns"
rm -rf "$IS"

cat > "$APP/Contents/MacOS/pomocnik" <<'EOF'
#!/bin/sh
# Ścieżka do tego pliku trafia do autostartu, żeby po przeniesieniu aplikacji wszystko działało dalej.
export POMOCNIK_LAUNCHER="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
exec /usr/bin/python3 "$DIR/pomocnik.py" "$@"
EOF
chmod +x "$APP/Contents/MacOS/pomocnik"

WERSJA=$(sed -n "s/^WERSJA = '\(.*\)'/\1/p" pomocnik.py)
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Pomocnik rezerwacji</string>
  <key>CFBundleDisplayName</key><string>Pomocnik rezerwacji</string>
  <key>CFBundleIdentifier</key><string>pl.pomocnik-rezerwacji</string>
  <key>CFBundleExecutable</key><string>pomocnik</string>
  <key>CFBundleIconFile</key><string>ikona</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$WERSJA</string>
  <key>CFBundleVersion</key><string>$WERSJA</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

(cd dist && rm -f Pomocnik-rezerwacji-mac.zip && ditto -c -k --keepParent "Pomocnik rezerwacji.app" Pomocnik-rezerwacji-mac.zip)
echo "Gotowe: $APP"
echo "Paczka: dist/Pomocnik-rezerwacji-mac.zip"
