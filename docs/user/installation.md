# Installation guide

The illustrated guides are available in Japanese:

- [インストール説明書 — illustrated installation guide](インストール説明書.md)
- [概要説明書 — overview, requirements, limitations and risks](概要説明書.md)
- [取扱説明書 — operation, security and troubleshooting](取扱説明書.md)

Use the published stable release assets, `raspi-recorder-X.Y.Z.tar.zst` and
`SHA256SUMS`, from [GitHub Releases](https://github.com/raspikoubou/raspi-recorder/releases/latest).
The guide covers Windows/macOS preparation, Raspberry Pi OS Lite 64-bit
(Debian 13/Trixie), Raspberry Pi 5, Python 3.13, download or SCP transfer,
checksum verification, installation, first startup, password protection,
recording verification, automatic startup and updates.

Writing the OS erases the selected card. For existing installations, follow the
software update instructions instead. Initial installation requires Internet
access from the Pi for OS packages and Python source builds. The installer
preserves recordings, settings and authentication data. It does not start or
enable the recording and web services automatically.

The standard web interface uses unencrypted HTTP on port 8080. Use a trusted LAN
or protected VPN and do not expose it directly to the Internet. Login and
administrator protection are initially off. Configure protection before normal
use. Recording completeness and successful recovery are not guaranteed. Save
important recordings to a separate device and verify the copies.
