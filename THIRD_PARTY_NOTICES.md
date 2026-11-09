# Third-party notices

Clip Squeezer's own code is under the MIT License. This file covers the
third-party software in the Windows release zip, and the FFmpeg download the app
offers. The `licenses/` paths below are inside the zip.

## FFmpeg (not included)

Neither this repository nor the release zip contains FFmpeg.

The first time you squeeze a clip, the app asks to download `ffmpeg.exe` and
`ffprobe.exe` from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds).
If you say yes, they come straight from that project's GitHub releases to your
PC. You can also use your own copy by putting it on your `PATH`.

Those builds are licensed under the GNU General Public License version 3 or
later. Their licence terms and source are provided by the
[FFmpeg project](https://ffmpeg.org/legal.html) and by BtbN/FFmpeg-Builds.
Clip Squeezer runs FFmpeg as a separate program and isn't linked with it.

FFmpeg is a trademark of Fabrice Bellard. Clip Squeezer is not affiliated with
the FFmpeg project.

## Included in the release zip

| Component | Licence | Text |
| --- | --- | --- |
| Python runtime | Python Software Foundation License | `licenses/Python-LICENSE.txt` |
| Tcl/Tk | Tcl/Tk License (BSD-style) | `licenses/Tcl-Tk-LICENSE.txt` |
| tkinterdnd2 (drag and drop) | MIT License | `licenses/tkinterdnd2-LICENSE.txt` |
| PyInstaller bootloader | GPL 2.0 or later, with an exception that allows use in programs under other licences | [pyinstaller.org](https://pyinstaller.org/en/stable/license.html) |

Any other Python support files PyInstaller adds to `_internal` keep their own
licence metadata there.
