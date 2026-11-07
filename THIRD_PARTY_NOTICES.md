# Third-party notices

## Application and web assets

The application source in this project is licensed under the MIT License; see
`LICENSE`. No third-party JavaScript playback bundle is included.

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

Primary references for this installation route:

- [PyAV 15.0.0 installation requirements](https://pyav.basswood-io.com/docs/15.0/overview/installation.html)
- [PyAV 15.0.0 source metadata and Python 3.13 classifier](https://raw.githubusercontent.com/PyAV-Org/PyAV/v15.0.0/pyproject.toml)
- [PyAV 15.0.0 license](https://github.com/PyAV-Org/PyAV/blob/v15.0.0/LICENSE.txt)
- [Debian Trixie arm64 FFmpeg package](https://packages.debian.org/trixie/arm64/ffmpeg)
- [Debian Trixie arm64 libavcodec61 dependencies](https://packages.debian.org/trixie/arm64/libavcodec61)
- [Debian Trixie arm64 x264 package](https://packages.debian.org/trixie/arm64/libx264-164)
- [x264 licensing overview](https://x264.org/licensing/)
