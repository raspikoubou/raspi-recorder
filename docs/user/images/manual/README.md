# 説明書の画像について

## Web画面のスクリーンショット

`live-mobile.png`、`live-desktop.png`、`settings-home-simple.png`、`settings-camera-simple.png` は、常時録画専用の現行UIをChromeで撮影しました。モバイルは390×844px、PCは1280×900pxです。`recordings-picker-simple.png` は前回の同じ日付・時間選択UIの撮影画像です。隔離したWeb API fixtureを使い、日付・機器・設定値は表示例です。fixtureの録画内容は模擬データであり、これらは実録画の再生を検証する画像ではありません。画面の文字・DOMを書き換える加工はしていません。

`imager-`を除くその他の画像は従来の0.1.33（基準 `b1252928ffc9a7316a830216c540ab85508fdb66`）をPlaywright／Chromiumで撮影したものです。合成カメラ入力を実際の録画engine・Web APIへ接続した過去版の撮影記録です。旧録画一覧等の画像は現行の操作説明には使用しません。

物理的なRaspberry Pi・USBカメラ・USB保存媒体・マイクは接続していません。赤・青の映像、仮想機器名、容量、日時、件数、設定値は説明用です。320×240・5fps、音声OFF、日時焼き込みOFF、容量閾値等は撮影用の値で、製品の初期値や推奨値ではありません。

時刻変更helperは撮影環境では利用できず、その状態を表示しています。同期未確認・警告・保護OFFの画像は、それらを正常な運用状態として推奨するものではありません。更新画面は安定版確認を実行する前の状態です。実機での録画性能・配線・更新・復旧の検証を示す画像ではありません。

画面データの文字を書き換える加工や警告の除去は行っていません。一部は操作項目を見やすくするため画面要素単位で撮影し、画面幅・高さを調整しています。日付入力や動画プレイヤーはブラウザ・OSで見え方が異なります。

開発環境で撮影し直す場合は、リポジトリ直下で実行します。Pythonの開発・ブラウザ依存、Chromium、1.3GB以上の一時空き容量、ローカルTCP／Unixソケットが使える環境が必要です。

```sh
python -m pip install -e '.[dev,browser]'
python -m playwright install chromium
TZ=Asia/Tokyo python -m tests.web.capture_manual
```

実運用データは使わず、一時ディレクトリーに録画・DBを作成し、終了時に閉じて削除します。画像の出力先はこのフォルダーです。撮影用スクリプトとテストは開発リポジトリにのみ含み、製品アーカイブには含みません。

## Raspberry Pi Imagerの参考画像

次の画像は、同じ公開元の[24V Event Loggerリポジトリ](https://github.com/raspikoubou/24v-event-logger-oss/tree/fdaf861d75556fafbde579f84557cbef18786c57/docs/images/manual)から、そのまま再利用しています。

- `imager-device.png`：Raspberry Pi 5の選択。
- `imager-os-menu.png`：OS一覧。
- `imager-os-lite.png`：Raspberry Pi OS Lite 64-bitの選択。

参照コミット：`fdaf861d75556fafbde579f84557cbef18786c57`。Imagerの版により表示名・順番が異なります。Debian 13（Trixie）・Lite・64-bitという選択条件を本文で確認してください。

元リポジトリの著作権・ライセンス表記は以下のとおりです。Raspberry Pi等の名称・表示は各権利者に帰属します。

```text
MIT License

Copyright (c) 2026 らずぱい工房

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
