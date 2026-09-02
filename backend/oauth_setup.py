"""
Google OAuth 2.0 授權流程工具
用於取得 Google Drive API 的 refresh token

使用方式：
  1. 先在 Google Cloud Console 建立 OAuth 2.0 用戶端 ID（桌面應用程式）
  2. 執行：python oauth_setup.py --client-id "xxx.apps.googleusercontent.com" --client-secret "xxx"
  3. 瀏覽器會自動開啟，選擇 Google 帳號並授權
  4. 授權完成後，程式會輸出 refresh token
  5. 把 refresh token 存入環境變數 GOOGLE_OAUTH_REFRESH_TOKEN
"""
import argparse
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    parser = argparse.ArgumentParser(description="Google OAuth 2.0 授權流程")
    parser.add_argument("--client-id", required=True, help="OAuth 2.0 用戶端 ID")
    parser.add_argument("--client-secret", required=True, help="OAuth 2.0 用戶端密鑰")
    parser.add_argument("--output", default=None, help="輸出檔案路徑（預設僅輸出到螢幕）")
    args = parser.parse_args()

    # 建立 client_secret.json 格式的設定（InstalledAppFlow 需要）
    client_config = {
        "installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("=" * 60)
    print("Google OAuth 2.0 授權流程")
    print("=" * 60)
    print(f"\n用戶端 ID: {args.client_id}")
    print(f"請求範圍: {SCOPES}")
    print("\n即將開啟瀏覽器進行授權...")
    print("請選擇要用來上傳相片的 Google 帳號（建議：lin61751226@gmail.com）")
    print("並按「允許」授權存取 Google Drive。")
    print()

    # 跑授權流程（本機有瀏覽器，用 run_local_server）
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)

    print("\n" + "=" * 60)
    print("授權成功！")
    print("=" * 60)
    print(f"\nRefresh Token:")
    print(f"{creds.refresh_token}")
    print(f"\nClient ID: {args.client_id}")
    print(f"Client Secret: {args.client_secret}")
    print()

    # 輸出到檔案
    if args.output:
        output_data = {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "refresh_token": creds.refresh_token,
            "scopes": SCOPES,
        }
        output_path = Path(args.output)
        output_path.write_text(json.dumps(output_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已儲存到: {output_path}")

    print("\n請將以下三個值存入環境變數：")
    print(f"  GOOGLE_OAUTH_CLIENT_ID={args.client_id}")
    print(f"  GOOGLE_OAUTH_CLIENT_SECRET={args.client_secret}")
    print(f"  GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
