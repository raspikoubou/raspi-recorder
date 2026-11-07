# 配布物と導入

GitHub Releaseには、製品ソースと利用者向け説明を含む`raspi-recorder-X.Y.Z.tar.zst`と、archiveのSHA-256を記した`SHA256SUMS`を配置します。checksumを確認してから展開・導入してください。SHA256SUMSは署名ではなく、公開者の本人確認を行いません。

buildは試行ごとに別の空の出力directoryで行い、同じ出力先へ同時実行しないでください。生成中にsourceが変更されて内部SHA256SUMSと不一致になった場合は、成果物の公開前に失敗します。

初回導入ではPiがインターネットへ接続し、APTからOSのbuild dependency、PyPIから固定したPython source packageを取得します。Pi上で各dependencyをbuildします。Python wheelやFFmpeg binaryはRelease archiveへ含めません。Piへ直接ダウンロードする方法と、PCからSCP転送する方法は[インストール説明書](../docs/インストール説明書.md)に記載しています。

導入後は、カメラ・保存先・認証保護を確認してからsystemd serviceを有効化してください。Installerはengine/Webを自動起動しません。設定画面から安定版を更新する場合、Release archiveとSHA256SUMSを検証してから切替を行います。切替失敗時は以前のversionとdatabaseへ戻します。更新中は録画が一時停止します。

新規導入時はログイン保護と管理者保護がOFFです。利用環境に合った保護を設定してください。HTTPは暗号化されないため、信頼できるLANまたはVPN内で利用し、port 8080をインターネットへ公開しないでください。

本体ソースはMIT Licenseです。PyAV、FFmpeg、x264、その他のPython/OS dependencyはそれぞれのlicense条件に従います。[第三者ライセンス表記](../THIRD_PARTY_NOTICES.md)を確認してください。アプリから録画・設定・認証情報を削除せずに導入します。
