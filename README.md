# らずぱいドライブレコーダー

Raspberry PiとUSBカメラで映像を記録し、LAN内のWebブラウザからライブ表示・検索・再生・持ち出しを行う一般用途のソフトウェアです。録画engineとWeb UIを分離しています。

## 主な機能

- 任意の省電力モード（対応profile内で解像度・fpsを下げ、音声を停止。通常設定は保持）
- 録画fps・画質と独立した、トップ画面の1fps自動画像プレビュー
- MP4/H.264録画、明示操作時の音声付きライブ、録画一覧・検索・再生・seek
- 正常確定MP4のZIP一括ダウンロード、異常録画の個別ダウンロード
- microSDまたは対応するUSB媒体への保存、容量管理、ログイン用／管理者用password

配布形式は、製品ソースと図解・スクリーンショット付きの利用者向け説明書を含む`.tar.zst`と`SHA256SUMS`です。Python依存wheelはアーカイブへ含めず、Pi上でソースから構築します。PyAVはDebian TrixieのFFmpegと組み合わせるため`15.0.0`に固定しています。公開安定版は[GitHub Releases](https://github.com/raspikoubou/raspi-recorder/releases/latest)から取得します。

省電力モードは初期値OFFです。ONにするとカメラが申告する対応profileから現在値を超えない解像度・fpsを選び、音声を停止します。通常設定は保存したままなので、OFFに戻せば通常設定へ戻ります。低負荷化の程度は機器や構成によって異なります。

新規導入時はログイン保護・管理者保護ともOFFで、パスワード登録なしにトップ画面と設定を利用できます。設定画面から必要な保護だけを個別に有効化します。ログイン保護は閲覧・録画操作・MP4／ZIP取得を、管理者保護は設定保存・録画削除・時刻変更・更新適用・認証変更を守ります。どちらもOFFなら、それらの操作にパスワードを求めません。認証OFFでもCSRF・Origin・Host検査は維持します。既存登録済みDBのhash・ログイン設定・録画は移行時に保持します。HTTPのため信頼できるLANまたはVPN内で利用してください。画面上部からライブ・録画・設定を移動できます。

## 基準環境

- Raspberry Pi 5、RAM 4GB以上
- Raspberry Pi OS Lite 64-bit（Debian 13）
- Python 3.13

これは初版の基準環境であり、全てのカメラ・電源・保存媒体の性能を保証しません。

## 導入と操作

初めて使う方に向けた3冊の説明書を用意しています。

| 説明書 | 内容 |
|---|---|
| [概要説明書](docs/user/概要説明書.md) | 用途・構成図・必要機器・録画の仕組み・制約と免責 |
| [インストール説明書](docs/user/インストール説明書.md) | OS用SD作成から、公開Release取得・導入・初回設定・動作確認まで |
| [取扱説明書](docs/user/取扱説明書.md) | 画面操作・設定・録画の持ち出し・セキュリティ・更新・点検・トラブル対応 |

初回導入にはインターネット接続が必要です。インストーラーはsystemd serviceを自動起動しません。カメラと保存先を確認し、必要な認証保護を設定してから次回起動時の自動開始を有効にしてください。各説明書では、操作する機器・入力例・成功時の表示と、録画中断やデータ消失などの注意を説明しています。

## 録画データとネットワーク

本ソフトウェアは安全装置・制御装置ではなく、録画の連続性、欠損のない保存、事故時の確実な記録を保証しません。重要な映像は別媒体へバックアップしてください。

Web UIはHTTPです。信頼できるLANまたはVPN内だけで使い、インターネットへ直接公開しないでください。録画映像や利用状況を開発元へ送信しません。

USB録画先はext4またはexFATに限ります。FAT32/NTFSは対応せず、アプリからフォーマットしません。USB上の既存データは録画管理の対象外です。

## 開発時の確認

USB映像入力とALSA音声入力はOSのFFmpegを別プロセスで起動して取得します。録画とHLSのエンコードにはPyAVを使用します。開発環境にも`ffmpeg`を用意してください。

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
node --check src/raspi_recorder/static/app.js
bash -n packaging/install.sh
```


ブラウザ試験では、合成カメラ2台と実際の録画engine・Web APIを起動し、Chromiumで設定保存・認証・録画再生・MP4/ZIP取得・削除と画面幅320/390/768/1440pxでの操作を確認します。以下を追加で実行してください。機器が見つからない場合などの試験では、ブラウザが受け取る能力情報だけを差し替えます。

```sh
python -m pip install -e '.[dev,browser]'
python -m playwright install chromium
pytest -q tests/web/test_browser.py
pytest -q tests/acceptance/test_virtual_two_camera_engine.py
```


## ライセンス

本体ソースのライセンスは[MIT License](LICENSE)です。アーカイブにはHLS.jsのライセンス表記を含みます。PyAVなどのPython依存はソース配布物をPi上で構築し、FFmpegとcodecはOSのAPTパッケージを利用します。各依存のライセンス条件は[第三者ライセンス表記](THIRD_PARTY_NOTICES.md)を確認してください。MIT表記は第三者物の条件を置き換えません。
