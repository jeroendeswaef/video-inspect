#!/bin/sh
# Install the launcher entry and icon for the current user, so the app shows its icon
# in the dock / app grid. Run once: ./install.sh
set -e
dir="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$dir/icons/hicolor/scalable/apps/nl.local.VideoInspect.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/"
sed "s#^Exec=.*#Exec=python3 $dir/video_inspect.py %f#" "$dir/nl.local.VideoInspect.desktop" \
    > "$HOME/.local/share/applications/nl.local.VideoInspect.desktop"
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed. (Remove the two copied files from ~/.local/share to uninstall.)"
