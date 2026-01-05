# libwebpac 爬蟲

使用 requests + BeautifulSoup 的簡易爬蟲，用來查詢 https://www.libwebpac.yuntech.edu.tw/ 並擷取搜尋結果。

安裝相依：

```bash
pip3 install -r requirements.txt
```

範例執行：

```bash
python3 libwebpac_scraper.py "關鍵字"
```

輸出為 JSON，包含 `query`、`count` 與 `results`（每項有 `title` 與 `link`）。

說明：程式會先嘗試自首頁找尋搜尋表單並提交；若找不到表單，會改以首頁內容中的連結或常見結果格式解析。網站結構若改變，解析策略也可能需更新。
