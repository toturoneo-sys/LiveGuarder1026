# LiveGuarder1026 Windowsアプリ化

## まずやること
1. このフォルダをWindows PCへコピー
2. `token.json` を `LiveGuarder1026.py` と同じ場所に置く
3. `build_exe.bat` をダブルクリック
4. 完成した `dist\LiveGuarder1026.exe` を確認

## 重要
- `client_secret.json` は通常の起動には不要です。OAuth認証を作り直す場合だけ使用します。
- `token.json` は認証情報なので配布物に含めないでください。
- 配布前にアプリの「記録を全削除」を実行し、ユーザーの記録が残っていないことを確認してください。
- AIは現在未接続です。AIアダプターは後から接続できます。
- YouTubeの実操作は現在無効です。
- テスト専用スクリプトはこの本番フォルダには含めていません。

## 配布時の基本構成
LiveGuarder1026.exe
（初回認証・設定は別途用意）

※ `token.json` を同梱したまま配布しないでください。
