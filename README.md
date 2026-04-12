# 還書機前端 (Library Return Kiosk)

簡短說明：本專案為還書機前端與後端整合的 Flask 應用，負責掃描條碼、跟圖書館 SIP2 通訊、控制還書機硬體、以及紀錄本地還書箱。此 README 以繁體中文說明主要檔案與系統流程。

**主要目錄與檔案**
- `app.py`：Flask 應用啟動點（若有），用於建立 app 與註冊 blueprints。
- `shared.py`：專案共用設定、logger 與全域物件初始化（`machine`, `sip2`），以及日誌/DB 初始化函式。
- `config.yml`：運行時設定（例如 `book_check_enabled`、library 參數與 logs 設定）。
- `routes/`：Flask 路由
  - `api.py`：主要 API（掃描 `/scan`、還書 `/return`、檢查 `/check_due` 已停用等）、硬體控制介接點。
  - `admin_api.py`：後台 API（設定、參數更新等）。
  - `hardware_api.py`：硬體專用 API（open/close/wake 等）。
  - `views.py`：前端頁面路由（admin、return_book 等）。
- `templates/`：Jinja2 模板（`return_book.html`, `admin.html` 等）與 `templates/partials/*` 的前端腳本片段。
- `static/`：靜態資源（CSS、上傳檔案、圖片等）。
- `sip2_client.py`：SIP2 協定的客戶端實作（`get_book_info`, `checkin_book`, `health_check`），解析 AH/AQ/AR 等欄位。
- `machine_controller.py`：還書硬體控制器邏輯（序列埠通訊、state 機制、open/close/sort 等）。
- `return_box.db`：本地 SQLite 資料庫（`box_inventory`, `box_history`），此檔通常不應加入版本控制。
- `RELEASE_NOTES.md`：釋出說明。

**日誌結構**
- `logs/app/app.log` & `logs/app/history/`：App 日誌與歷史檔案（每日輪轉至 history）。
- `logs/library/library.log` & `logs/library/history/`：圖書館/SIP2 相關日誌。
- `logs/machine/machine.log` & `logs/machine/history/`：硬體日誌。

（輪轉行為：當日的最新日誌保留在 `logs/<name>/<name>.log`，舊檔移到 `logs/<name>/history/`，檔名會包含日期，便於逆向排序查看最新記錄。）

**核心流程（簡要流程圖）**

1. 使用者掃描/輸入條碼（前端）
2. 前端呼叫 `/api/scan` → `routes/api.py` 處理
   - 後端透過 `sip2_client.get_book_info(barcode)` 取得 AH/AQ/AR 等欄位
   - 若 AH 表示「在館內」→ 回傳 `ALREADY_IN_LIBRARY`（阻擋還書）
   - 依 AR/AQ 決定附件狀態：
     - AR 空 或 AR = "附件未借出" → 視為可由機器處理，允許還書
     - 其他 AR 非空 或 AQ 表示附件 → 本機不處理，請至櫃台
3. 若允許還書 → 呼叫硬體流程（open -> 放書 -> close -> check book status）
4. 若硬體與 SIP2 都成功 -> 將記錄寫入 `return_box.db` 的 `box_inventory`
5. 若分類機制啟用 -> 執行 `machine.sort_book(target_bin)` 把書送到分櫃

ASCII 流程圖（簡化）

```
[使用者掃描] -> [前端] -> POST /api/scan
                 -> [SIP2 查詢 (sip2_client.get_book_info)]
                   -> 解析 AH/AQ/AR
                   -> 若 "在館內" => 回傳 ALREADY_IN_LIBRARY
                   -> 若 有不可處理附件 => 回傳 ATTACHMENT_NOT_ACCEPTED
                   -> 否則 -> 回傳掃描結果 (不含到期日)
                 -> 前端顯示結果 -> 使用者確認 -> /api/return
                 -> [機器流程] open -> 放書 -> close -> check
                 -> [SIP2 checkin] -> 若成功寫入 local DB -> sort_book -> 完成
```

**常見操作**
- 啟動伺服器（範例）:
```bash
source .venv/bin/activate
export FLASK_APP=app.py
flask run --host=0.0.0.0 --port=5000
```
- 編輯 `config.yml` 後重啟服務以套用設定（例如 `book_check_enabled`, `machine.port`）。

**安裝前需求**
- Linux 環境，且可使用 `systemctl --user`
- `systemctl --user` 必須在目標使用者的登入 session / user bus 可用時執行；若看到 `Failed to connect to bus: No such file or directory`，請先登入該使用者再安裝，或先執行 `sudo loginctl enable-linger <user>`
- 已安裝 `python3` 與 `pip`
- 目標使用者可寫入 `~/.config/systemd/user/`
- 若要安裝 Python 依賴，需可連網或已準備離線套件來源
- 若要啟用瀏覽器 kiosk 模式，系統需已存在對應的 Firefox 與圖形桌面環境

**第一次部署步驟**
1. 將專案放到正確位置，並確認 `requirements.txt`、`systemd/user/*.service`、`install_user_services.sh` 都在 repo 內。
2. 進入專案目錄，執行：
```bash
./install_user_services.sh
```
3. 腳本會安裝 Python 依賴，並把 service 複製到使用者的 `~/.config/systemd/user/`。
4. 確認服務狀態：
```bash
systemctl --user status pyserver.service
systemctl --user status kiosk-browser.service
```
5. 若需重新載入設定或重裝：
```bash
systemctl --user daemon-reload
systemctl --user restart pyserver.service kiosk-browser.service
```

**一鍵安裝（建議）**

專案內提供 `install_user_services.sh`，可同時安裝 systemd user services 與 Python 依賴。

```bash
# 全部安裝
./install_user_services.sh

# 只安裝 service
./install_user_services.sh --services-only

# 只安裝 Python 依賴
./install_user_services.sh --python-only

# 略過 Python 依賴，只做 service
./install_user_services.sh --skip-python-deps
```

安裝後會把以下服務複製到使用者的 `~/.config/systemd/user/` 並啟用：
- `pyserver.service`
- `kiosk-browser.service`

Python 依賴則會依照 `requirements.txt` 安裝。

**注意事項與建議**
- 請不要把 `return_box.db` 上傳到 public repository；若要上傳至內部 GitLab，請先確認是否需過濾或匿名化敏感資料。
- 若需回滾敏感檔案（從 commit 歷史移除），請在上傳前通知，我可協助使用 `git filter-repo` 進行清理。
- 若要啟用自動 `git push`（每次 commit 後自動上傳），可在本地 repo 加上 `.git/hooks/post-commit` 腳本，但需先確保 SSH/HTTP 認證與網路可用。

**聯絡 / 支援**
- 若需我幫你把本地 commit 推上 GitLab，請貼上你在 GitLab 上的專案 Git URL（SSH 或 HTTP）並確認你的 SSH key/Token 已於 GitLab 註冊。

---