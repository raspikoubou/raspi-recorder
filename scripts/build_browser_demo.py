#!/usr/bin/env python3
"""Build the browser demo from the product UI and generated conveyor scenes."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs/demo"
parser = argparse.ArgumentParser()
parser.add_argument("--media", action="store_true")
args = parser.parse_args()

for name in ("app.js", "app.css"):
    shutil.copyfile(ROOT / "src/raspi_recorder/static" / name, DEMO / name)
source = (ROOT / "src/raspi_recorder/templates/index.html").read_text()
source = source.replace("{{ url_for('static', filename='app.css') }}", "./app.css")
source = source.replace("{{ url_for('static', filename='app.js') }}", "./app.js")
source = source.replace(
    '    <script src="./app.js"',
    '    <link rel="stylesheet" href="./demo.css">\n'
    '    <script src="./demo.js" defer></script>\n'
    '    <script src="./app.js"',
)
source = source.replace(
    "  <body>",
    """  <body>
    <aside class="demo-notice" aria-label="体験デモ">
      <span><strong>体験デモ</strong> · サンプル映像で操作できます。
        変更はページを開き直すと元に戻ります。</span>
      <span class="demo-links">
        <button id="demo-reset" type="button">初期状態に戻す</button>
        <a href="https://github.com/raspikoubou/raspi-recorder/tree/main/docs/user"
           target="_blank" rel="noopener">説明書</a>
      </span>
    </aside>""",
)
(DEMO / "index.html").write_text(source)

if args.media:
    import imageio_ffmpeg
    from PIL import Image, ImageDraw, ImageFont

    executable = imageio_ffmpeg.get_ffmpeg_exe()
    regular = ImageFont.load_default(size=23)
    large = ImageFont.load_default(size=34)
    for camera in (1, 2):
        output = DEMO / f"media/sample-camera-{camera}.mp4"
        command = [
            executable,
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            "1280x720",
            "-r",
            "15",
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-threads",
            "2",
            str(output),
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        for frame in range(225):
            t = frame / 15
            im = Image.new("RGB", (1280, 720), "#d8e1e7")
            d = ImageDraw.Draw(im)
            d.rectangle((0, 0, 1280, 350), fill="#c4d0d8")
            for x in range(0, 1280, 160):
                d.line((x, 0, x, 350), fill="#9aaeb9", width=3)
            d.rectangle((0, 350, 1280, 720), fill="#879ba5")
            d.polygon([(0, 520), (1280, 470), (1280, 590), (0, 640)], fill="#4d6573")
            d.polygon([(0, 495), (1280, 445), (1280, 480), (0, 530)], fill="#314753")
            for n in range(28):
                x = (n * 55 + t * 90) % 1400 - 60
                y = 535 - x * 0.039
                d.line((x, y, x, y + 70), fill="#a6bac2", width=4)
            for n in range(5):
                x = (n * 310 + t * (90 if camera == 1 else 120)) % 1600 - 200
                y = 480 - x * 0.039
                color = "#cfa366" if n != 2 else "#739fa9"
                d.polygon(
                    [(x, y), (x + 150, y - 6), (x + 150, y - 100), (x, y - 94)],
                    fill=color,
                )
                d.polygon(
                    [
                        (x, y - 94),
                        (x + 35, y - 125),
                        (x + 185, y - 131),
                        (x + 150, y - 100),
                    ],
                    fill="#e4bd85",
                )
                d.polygon(
                    [
                        (x + 150, y - 6),
                        (x + 185, y - 37),
                        (x + 185, y - 131),
                        (x + 150, y - 100),
                    ],
                    fill="#a57b48",
                )
                d.rectangle((x + 44, y - 72, x + 106, y - 35), fill="#f0ebdf")
                d.text((x + 48, y - 69), f"{n + 1:03}", font=regular, fill="#314753")
            d.rectangle((760, 110, 815, 455), fill="#31515c")
            d.rectangle((710, 105, 865, 155), fill="#20424c")
            d.ellipse((770, 170, 805, 205), fill="#2dd4a5")
            d.rounded_rectangle((905, 175, 1195, 350), radius=12, fill="#193c47")
            d.text(
                (930, 195),
                "LINE 01" if camera == 1 else "INSPECTION",
                font=large,
                fill="#e5f2f5",
            )
            d.text(
                (930, 250), f"COUNT {120 + int(t * 1.4):04}", font=large, fill="#54d7b1"
            )
            d.text((930, 300), "RUNNING", font=regular, fill="#b0d7de")
            if camera == 2:
                crop = im.crop((320, 150, 1190, 640))
                im = crop.resize((1280, 720))
                d = ImageDraw.Draw(im)
            d.rectangle((0, 0, 1280, 72), fill="#102a36")
            d.text(
                (24, 18),
                f"SAMPLE FOOTAGE   |   CAMERA {camera}",
                font=large,
                fill="white",
            )
            d.rectangle((0, 670, 1280, 720), fill="#102a36")
            d.ellipse((24, 686, 40, 702), fill="#f66a6a")
            d.text(
                (55, 680),
                f"2026-01-15 09:00:{int(t):02d}    REC",
                font=regular,
                fill="white",
            )
            d.text((905, 680), "GENERATED SAMPLE", font=regular, fill="#9ab8c4")
            if frame == 0:
                im.save(DEMO / f"media/poster-camera-{camera}.jpg", quality=86)
            process.stdin.write(im.tobytes())
        process.stdin.close()
        if process.wait():
            raise SystemExit("Sample video encoding failed")
manifest = {
    name: hashlib.sha256((DEMO / name).read_bytes()).hexdigest()
    for name in (
        "app.js",
        "app.css",
        "media/sample-camera-1.mp4",
        "media/sample-camera-2.mp4",
    )
    if (DEMO / name).exists()
}
(DEMO / "source.json").write_text(
    json.dumps(
        {
            "version": "0.1.44",
            "source_commit": "64682e5c86820d51c1584d3f527daef7388b2a6c",
            "sha256": manifest,
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n"
)
