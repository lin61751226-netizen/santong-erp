"""
OAuth 2.0 授權流程 - 取得 Google Drive refresh token

使用方式：
  python oauth_authorize.py --client-id <CLIENT_ID> --client-secret <CLIENT_SECRET>

或設定環境變數：
  set GOOGLE_OAUTH_CLIENT_ID=<CLIENT_ID>
  set GOOGLE_OAUTH_CLIENT_SECRET=<CLIENT_SECRET>
  python oauth_authorize.py

執行後會開啟瀏覽器，請選擇 lin61751226@gmail.com 帳號並授權
"""
import argparse
import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
OUTPUT_FILE = Path(__file__).parent / "oauth_credentials.json"


def parse_args():
    parser = argparse.ArgumentParser(description="Google OAuth 2.0 授權流程")
    parser.add_argument("--client-id", help="OAuth 用戶端 ID（或設定 GOOGLE_OAUTH_CLIENT_ID 環境變數）")
    parser.add_argument("--client-secret", help="OAuth 用戶端密鑰（或設定 GOOGLE_OAUTH_CLIENT_SECRET 環境變數）")
    return parser.parse_args()


def main():
    args = parse_args()

    client_id = args.client_id or os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = args.client_secret or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("錯誤：必須提供 client_id 和 client_secret")
        print("\n使用方式：")
        print("  python oauth_authorize.py --client-id <CLIENT_ID> --client-secret <CLIENT_SECRET>")
        print("\n或設定環境變數：")
        print("  set GOOGLE_OAUTH_CLIENT_ID=<CLIENT_ID>")
        print("  set GOOGLE_OAUTH_CLIENT_SECRET=<CLIENT_SECRET>")
        print("  python oauth_authorize.py")
        sys.exit(1)

    print("=" * 60)
    print("Google OAuth 2.0 授權流程")
    print("=" * 60)
    print(f"\n用戶端 ID: {client_id[:20]}...")
    print(f"請求範圍: {SCOPES}")
    print("\n即將開啟瀏覽器進行授權...")
    print("請選擇 lin61751226@gmail.com 帳號，並按「允許」授權存取 Google Drive。")
    print("如果瀏覽器沒有自動開啟，請手動複製終端機中顯示的 URL。")
    print()

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    try:
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    except Exception as e:
        print(f"\n授權失敗: {e}")
        print("\n可能原因：")
        print("1. 此應用程式尚未發布，需要先在 OAuth 同意畫面中加入測試使用者")
        print("2. 使用者取消了授權")
        print("3. 網路連線問題")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("授權成功！")
    print("=" * 60)
    print(f"\nRefresh Token:")
    print(f"{creds.refresh_token}")
    print(f"\nClient ID: {client_id}")
    print(f"Client Secret: {client_secret}")
    print(f"Token URI: {creds.token_uri}")
    print(f"Scopes: {creds.scopes}")

    output_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }
    OUTPUT_FILE.write_text(json.dumps(output_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n已儲存到: {OUTPUT_FILE}")

    print("\n請將以下三個值存入環境變數：")
    print(f"  GOOGLE_OAUTH_CLIENT_ID={client_id}")
    print(f"  GOOGLE_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"  GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
