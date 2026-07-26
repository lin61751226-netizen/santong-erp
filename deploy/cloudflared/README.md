# 固定網域建議做法

這個專案目前最適合的正式網址方案是：

1. 使用你自己的正式網域
2. 把 DNS 交給 Cloudflare
3. 建立 named tunnel
4. 將子網域指到本機的 `http://127.0.0.1:8000`

建議的正式網址格式：

- `https://line.你的網域`

最短切換流程：

1. 在 Cloudflare 建立 named tunnel
2. 把 tunnel 憑證 JSON 放到本機，例如 `C:\cloudflared\YOUR_TUNNEL_ID.json`
3. 依照 [config.example.yml](C:\Users\lin61\Documents\三通\deploy\cloudflared\config.example.yml) 建立正式 `config.yml`
4. 啟動 tunnel
5. 執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\switch-line-domain.ps1 -BaseUrl https://line.你的網域
```

這支腳本會同時做三件事：

- 更新 `.env` 與 `backend/.env` 的 `PUBLIC_BASE_URL`
- 重設 LINE 官方 webhook URL
- 重新發佈 Rich Menu，讓按鈕連回正式網域

如果你已經有固定主機，而不是要走 Cloudflare tunnel，也可以直接把正式 HTTPS 網址丟給 `switch-line-domain.ps1`。
