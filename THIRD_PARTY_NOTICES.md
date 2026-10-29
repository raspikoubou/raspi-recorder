# Third-party notices

## Application and web assets

The application source in this project is licensed under the MIT License; see
`LICENSE`. The browser playback bundle includes HLS.js v1.7.3 from the
`video-dev/hls.js` project under Apache License 2.0. Its complete license text
is `src/raspi_recorder/static/vendor/hls-LICENSE.txt`. The local license file
matches the upstream v1.7.3 `LICENSE` byte for byte; the locally bundled
`hls.min.js` SHA-256 is listed in `src/raspi_recorder/static/vendor/README.md`.
This asset is included in the product source distribution and is served locally
without a runtime CDN request.

## Source distribution and system dependencies

The distribution archive contains application source and does not contain Python
wheels or FFmpeg binaries. The installer downloads the pinned Python source
distributions from PyPI and builds them on the target Pi. PyAV is pinned to
15.0.0 (BSD-3-Clause for PyAV); its source-build documentation supports
FFmpeg 7.0 and later. At the time of review, Debian Trixie supplied FFmpeg
7.1.5 for arm64.
The installer obtains FFmpeg and its development libraries through APT, so
those OS packages and their transitive libraries remain subject to their own
license terms.

The application asks PyAV to encode H.264 using `libx264`. Debian's Trixie
`libavcodec61` package depends on the `libx264-164` shared library. x264 source
files state GPL version 2 or later. These binaries are installed by APT from
the OS repositories and are not copied into the project's distribution archive.
Users and system integrators must review the applicable Debian, FFmpeg, and
x264 notices for their system. The project's MIT license does not change
third-party terms. This notice identifies the dependency path; it is not a
legal determination that the combined application is distributable under MIT
alone. Integrators should review the applicable license terms and satisfy any
corresponding source and notice obligations for their distribution.

## Raspberry Pi 5 runtime observation (0.1.32, 2026-10-21)

On the tested Raspberry Pi 5 running Debian 13, the installed PyAV 15.0.0
reports BSD-3-Clause metadata and its `_core` extension dynamically links to
the APT-provided FFmpeg shared libraries. `ldd` also resolves `libx264.so.164`
and `libx265.so.215`; both encoders are available to the application. The
system `ffmpeg` 7.1.5 build reports `--enable-gpl`, `--enable-libx264`, and
`--enable-libx265`. `dpkg-query` identified the installed packages as
`ffmpeg` and `libavcodec61` `8:7.1.5-0+deb13u1+rpt2`, `libx264-164`
`2:0.164.3108+git31e19f9-2+b1`, and `libx265-215` `4.1-2`; `dpkg-query -S`
attributes the resolved shared libraries to these APT packages. The installed
FFmpeg package copyright files identify GPL-licensed components and describe
the default FFmpeg binaries as GPL-2-or-later. The recorder's H.264 path uses
x264; x265 was present as a linked system library but was not exercised as an
encoder in these recorder tests.

This live-device observation differs from both the source-only archive contents
and the historical PyAV 19.0.1 wheel audit below. It does not show that FFmpeg
libraries are bundled in the project archive. It does show that the installed
runtime links to system libraries built with GPL options and uses the
GPL-licensed x264 component for H.264 encoding. PyAV's package metadata alone
does not describe those linked libraries. The exact PyAV source archive digest
and build environment for this installed copy were not retained, so
binary-to-source provenance and the distribution obligations for the combined
product remain unresolved.
This is a factual dependency record, not a legal conclusion. The v0.1.33
source-only archive does not contain PyAV, FFmpeg, or x264 binaries; the Pi
installer obtains FFmpeg and its development libraries from APT and builds
PyAV from its pinned source distribution. The existing 0.1.32 Pi observation
does not establish the source digest or build environment of a future install.
This record does not classify a combined runtime or an appliance that a
downstream party may distribute. Anyone redistributing such a combined
runtime must review its actual build, component licenses, notices, and source
obligations. That downstream review is separate from what files this
source-only archive contains.

Primary references for these runtime components:

- [FFmpeg license and legal considerations](https://ffmpeg.org/legal.html)
- [FFmpeg external library notes for x264 and x265](https://www.ffmpeg.org/general.html)
- [PyAV project license and wheel notes](https://github.com/PyAV-Org/PyAV)
- [x264 licensing](https://images.videolan.org/developers/x264.html)
- [x265 licensing](https://x265.readthedocs.io/en/stable/introduction.html)

Primary references for this installation route:

- [PyAV 15.0.0 installation requirements](https://pyav.basswood.io/docs/15.0/overview/installation.html)
- [PyAV 15.0.0 source metadata and Python 3.13 classifier](https://raw.githubusercontent.com/PyAV-Org/PyAV/v15.0.0/pyproject.toml)
- [PyAV 15.0.0 license](https://github.com/PyAV-Org/PyAV/blob/v15.0.0/LICENSE)
- [Debian Trixie arm64 FFmpeg package](https://packages.debian.org/trixie/arm64/ffmpeg)
- [Debian Trixie arm64 libavcodec61 dependencies](https://packages.debian.org/trixie/arm64/libavcodec61)
- [Debian Trixie arm64 x264 package](https://packages.debian.org/trixie/arm64/libx264-164)
- [x264 licensing overview](https://x264.org/licensing/)

## Historical audit: prior wheelhouse package analysis

This section records an earlier ARM64/Python 3.13 wheelhouse investigation.
That wheelhouse is not part of the source distribution described above. The
following package and native-library details apply only to that earlier
wheelhouse package analysis.

The earlier wheelhouse analysis covered the following Python packages. The
candidate wheels preserved their license files and metadata under
`wheelhouse/`.

| Package | Version in the historical wheelhouse candidate | License indicated by the packaged wheel |
| --- | ---: | --- |
| PyAV (`av`) | 19.0.1 | BSD-3-Clause for PyAV source; see bundled native libraries below |
| Blinker | 1.9.0 | MIT |
| Click | 8.5.0 | BSD-3-Clause |
| Flask | 3.1.3 | BSD-3-Clause |
| gpiod | 2.5.0 | LGPL-2.1-or-later |
| Gunicorn | 26.2.0 | MIT |
| itsdangerous | 2.2.0 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| Pillow | 12.3.0 | MIT-CMU |
| setuptools | 84.0.0 | MIT; bundled vendored components retain their own license files |
| Werkzeug | 3.1.9 | BSD-3-Clause |

For packages whose wheel metadata did not state an SPDX expression, the
license was identified from that wheel's included license text.

## Native libraries bundled in the PyAV wheel

The pinned `av-19.0.1-cp312-abi3-manylinux_2_28_aarch64.whl` has SHA-256
`437d4c0d5a7d771f2c3af84cd28e6aac6e173851116c60b53e81dbf1eebe4eab`. The
PyAV project states that its binary wheels bundle FFmpeg. Inspection of this
exact wheel found FFmpeg 9.0.2 shared libraries and bundled libraries including
libx264, libx265, SVT-AV1, dav1d, libopus, libvpx, libwebp, libvmaf, GnuTLS,
ALSA, XCB, Xau, and DRM. PyAV's BSD-3-Clause metadata applies to PyAV; it does
not classify every bundled native library.

The pinned source archives contain the following license texts for the
upstream projects corresponding to libraries found in `av.libs/`. This table
identifies the source project's stated license; it is not a legal conclusion
about the combined wheel or proof that every binary file was built from the
matching source archive.

| Wheel library | Source archive in the source offer | License stated by the source project |
| --- | --- | --- |
| `libav*`, `libswresample`, `libswscale` | `ffmpeg-9.0.2.tar.xz` | FFmpeg is primarily LGPL-2.1-or-later; its license depends on enabled components and configure options. |
| `libx264` | `x264-b35605ace3ddf7c1a5d67a2eb553f034aef41d55.tar.bz2` | Source headers state GPL version 2 or later; FFmpeg 9.0.2 classifies libx264 under its GPL external-library list. |
| `libx265` | `x265_4.3.tar.gz` | Source headers state GPL version 2 or later; FFmpeg 9.0.2 classifies libx265 under its GPL external-library list. |
| `libSvtAv1Enc` | `SVT-AV1-v4.2.0.tar.bz2` | BSD 3-Clause Clear License. |
| `libdav1d` | `dav1d-1.5.4.tar.bz2` | BSD 2-Clause. |
| `libasound` | `alsa-lib-1.2.14.tar.bz2` | GNU LGPL version 2.1. |
| `libopus` | `opus-1.6.1.tar.gz` | BSD 3-Clause. |
| `libvmaf` | `vmaf-3.2.1.tar.gz` | BSD-2-Clause-Patent. |
| `libvpx` | `vpx-1.17.0.tar.gz` | BSD 3-Clause. |
| `libsharpyuv`, `libwebp`, `libwebpmux` | `webp-1.6.0.tar.gz` | BSD 3-Clause. |
| `libgmp` | `gmp-6.3.0.tar.xz` | GMP library is dual-licensed under LGPL version 3 or later or GPL version 2 or later. |
| `libgnutls` | `gnutls-3.8.13.tar.xz` | GnuTLS library files are LGPL version 2.1 or later; other project files are GPL version 3 or later unless noted otherwise. |
| `libnettle`, `libhogweed` | `nettle-4.0.tar.gz` | Nettle's source archive includes LGPL version 3 and GPL version 2/3 license texts; consult its file-level terms. |
| `libunistring` | `libunistring-1.4.2.tar.gz` | Library and headers are dual-licensed under LGPL version 3 or later or GPL version 2 or later. |

The same exact wheel also contains `libXau`, `libxcb`, `libxcb-shape`,
`libxcb-shm`, `libxcb-xfixes`, and `libdrm`. ELF debug-reference strings in
the wheel identify these build references: `libXau` 1.0.9-3.el8.aarch64,
`libdrm` 2.4.115-2.el8.aarch64, and `libxcb` plus its listed helper libraries
1.13.1-1.el8.aarch64. These embedded references identify package builds but do
not establish verified binary-to-source correspondence. The corresponding
source RPMs, their full license texts, and the manylinux image digest are not
included in the earlier source offer. Candidate source archives with these
names are listed in the [Red Hat UBI 8 AppStream source RPM index](https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi8/8/x86_64/appstream/source/SRPMS/Packages/l/):
`libXau-1.0.9-3.el8.src.rpm`, `libdrm-2.4.115-2.el8.src.rpm`, and
`libxcb-1.13.1-1.el8.src.rpm`. Their presence and matching package release do
not by themselves prove that they correspond to the wheel's exact binary
build.

FFmpeg 9.0.2's own `LICENSE.md` classifies libx264 and libx265 as GPL v2
external libraries and says combining either with FFmpeg requires
`--enable-gpl`. The pinned x264 and x265 source headers offer GPL version 2 or
later. The tagged `pyav-ffmpeg` 9.0.2-1 recipe lists `--enable-version3`
without `--enable-gpl`, and its `ffmpeg.patch` moves both libraries from
FFmpeg's GPL configure list to its version-3 list. This changes FFmpeg's
configure classification; it does not change the licenses in the source
archives. The exact binary's applicable terms and redistribution obligations
therefore remain unresolved and require a licensing determination before
distribution.

The embedded FFmpeg build-configuration string in the exact pinned ARM64
wheel's `libavcodec-3fb1106c.so.63.1.102` lists `--enable-version3`,
`--enable-libx264`, and `--enable-libx265`, and does not list `--enable-gpl`.
This is binary build metadata; it does not resolve the combined work's license
or redistribution conditions.

The historical local source-offer archive is named
`raspi-recorder-0.1.30-third-party-sources.tar.gz`. It contains the tagged
PyAV and `pyav-ffmpeg` source trees, the pinned build recipe and patches, the
upstream source archives used by that recipe, and the gpiod/setuptools source
distributions, with a SHA-256 inventory. The source archive does not contain
the exact source packages and license texts for every bundled platform
library: the wheel includes Xau, XCB helper libraries, and DRM libraries.
Embedded ELF references identify package build versions, but the matching
source RPMs and binary-to-source correspondence are not established. The
manylinux project describes its `manylinux_2_28` image as AlmaLinux 8 based,
but the exact image digest used for this wheel is not recorded. The
redistribution status of the patched libx264/libx265 build also has not been
confirmed. The license and source-correspondence gaps described here remain
unresolved for that exact wheel; do not use it as a redistribution asset. Do
not describe the bundled FFmpeg as OS-only, or the entire PyAV wheel as
BSD/MIT-only.

This earlier audit does not identify exact source correspondence for every
native library in that wheel. It does not describe or validate binaries in the
source distribution above.

Primary references for the earlier wheelhouse analysis:

- [HLS.js v1.7.3 release](https://github.com/video-dev/hls.js/releases/tag/v1.7.3)
- [HLS.js v1.7.3 license text](https://github.com/video-dev/hls.js/blob/v1.7.3/LICENSE)
- [PyAV installation documentation](https://pyav.basswood.io/docs/stable/overview/installation.html)
- [PyAV v19.0.1 release](https://github.com/PyAV-Org/PyAV/releases/tag/v19.0.1)
- [pyav-ffmpeg 9.0.2-1 source and build recipe](https://github.com/PyAV-Org/pyav-ffmpeg/tree/9.0.2-1)
- [FFmpeg 9.0.2 license and external-library classification](https://github.com/FFmpeg/FFmpeg/blob/n9.0.2/LICENSE.md)
- [pyav-ffmpeg 9.0.2-1 FFmpeg configure patch](https://github.com/PyAV-Org/pyav-ffmpeg/blob/9.0.2-1/patches/ffmpeg.patch)
- [pyav-ffmpeg 9.0.2-1 build recipe](https://github.com/PyAV-Org/pyav-ffmpeg/blob/9.0.2-1/scripts/build-ffmpeg.py)
- [manylinux image bases](https://github.com/pypa/manylinux#docker-images)
