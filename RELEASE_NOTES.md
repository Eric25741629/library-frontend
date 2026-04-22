# Release: 0.9.9 最終測試版

發布日期: 2026-04-22

狀態: Final Test / Kiosk security lockdown

重點說明：
- 重構 kiosk 全螢幕監控：舊版 shell X11 監控（`watch_fullscreen`）會在 Firefox 啟動期誤觸發
  `focus-lost` / `window-not-viewable` / `geometry-mismatch`，造成 2026-04-21 18:21 的重啟
  雪崩。改用前端 JS 的 `document.fullscreenElement` / `document.hasFocus()` / `visibilitychange`
  偵測，訊號乾淨無假警報。
- 新增焦點追蹤（資安需求）：視窗失焦 2.5 秒後若仍未拿回焦點則觸發重啟，並在啟動頭 15 秒保留緩衝
  避免 Firefox 拿焦點前被誤判。
- GNOME 快捷鍵鎖定：install 時用 `gsettings` 清空 40+ 個逃脫路徑（Super / Alt+Tab / Alt+F2 /
  Alt+F4 / Super+A / Super+1..9 / 截圖 / 登出 / 控制中心等）。
- 入侵視窗清理：後端收到 fullscreen_lost 時先 `pkill` 掉 12 種常見 GUI（gnome-control-center、
  nautilus、gnome-terminal、gnome-calculator、xterm 等）再 `systemctl restart kiosk-browser`，
  避免 Firefox 重生後入侵視窗還在前景。
- 簡化 `install_user_services.sh` 中的 browser wrapper：從 220+ 行 X11 polling 縮成 `while true;
  do firefox --kiosk URL; sleep 2; done`，systemd `Restart=always` 兜底。
- 新增 pytest 回歸測試套件（42 cases）：覆蓋後端 debounce/cooldown、前端 JS 合約（必須有 blur
  監聽 + hasFocus 二次確認 + 啟動緩衝）、GNOME lockdown install 正確性、入侵視窗 pkill 先於 restart。
- 端到端驗證：在實機啟動 gnome-calculator 覆蓋 Firefox，觀察到完整鏈路成功觸發（JS 偵測 →
  POST → pkill 計算機 → Firefox 重生）。

已知限制：
- Ctrl+Alt+F1..F6 切 TTY 與 Ctrl+Alt+Backspace 需改 `/etc/X11/xorg.conf.d/`（root 權限），
  本版未自動處理。

---

# Release: 0.9.4

發布日期: 2026-04-12

狀態: Stable / Deployment fix

重點說明：
- 修正 `pyserver.service` 與 `kiosk-browser.service` 的硬編碼路徑，安裝時會依目前使用者與專案實際路徑自動渲染。
- `install_user_services.sh` 會自動產生使用者家目錄下的 kiosk browser helper，降低跨機器部署失敗機率。
- `install_user_services.sh` 會在找不到 `systemd --user` bus 時輸出明確提醒，避免在沒有登入 session 的情況下安裝服務而直接失敗。
- 保留既有日誌分資料夾機制，並提醒遷移後需確保 [logs/](logs/) 目錄對執行者可寫。

已知風險/注意事項：
- 遷移到新電腦時，需重新執行 `install_user_services.sh` 以套用新的使用者與路徑。
- 若舊環境殘留 root 擁有的 [logs/](logs/) 或專案檔案，仍可能造成啟動失敗，請先修正權限。

---

# Release: 0.9.3 beta

發布日期: 2026-04-08

狀態: Beta（安裝與部署流程調整中）

重點說明：
- 新增 `install_user_services.sh`，可同時安裝 systemd user services 與 `requirements.txt` 內的 Python 依賴。
- 新增安裝選項，支援只安裝 services、只安裝 Python 依賴，或略過 Python 依賴。
- 補充 README 的安裝前需求、第一次部署步驟與一鍵安裝說明，讓其他人更容易照流程部署。

已知風險/注意事項：
- `kiosk-browser.service` 與 `pyserver.service` 內容仍使用固定路徑，若其他人安裝到不同使用者或不同目錄，需先調整 service 內路徑。
- 安裝 Python 依賴時需要可用的 `python3` 與 `pip`，且多半需要連網。

---

# Release: 0.9.1

發布日期: 2026-03-03

狀態: Stable

重點說明：
- 後台新增「還書機管理員帳號設定」獨立分頁，登入保護設定與一般機器設定分離。
- 管理員密碼欄位新增「顯示/隱藏」按鈕，輸入時可檢視目前內容。
- 新增「重複輸入密碼」驗證，兩次密碼不一致時禁止儲存。
- 修正登入頁焦點行為，避免密碼輸入時被自動跳回帳號欄位。
- 改善後台設定頁可捲動性，避免下方儲存按鈕無法操作。

已知風險/注意事項：
- 啟用登入保護後，若閒置逾時或 session 失效，需重新登入後才可再次儲存設定。
- 若僅更新帳號、不更新密碼，可將密碼欄位留空。

---

# Release: 0.9.0 beta

發布日期: 2026-01-15

狀態: Beta（可能有 bug）

重點說明：
- 停用 webpac 爬蟲與自動逾期阻擋，改為僅透過 SIP2 AH 欄位判斷「是否已在館內」，不再依日期封鎖還書。
- 調整附件判斷規則：
	- AR 為空 或 AR =「附件未借出」→ 視為本機可處理。
	- 其他 AR 非空文字 或 AQ 顯示附件說明 → 視為含附件，本機不處理，請至櫃檯。
- 日誌結構調整為 logs/app、logs/library、logs/machine 分資料夾，舊檔自動搬移至各自的 history/ 中，便於逆向檢視最新紀錄。

已知風險/注意事項：
- 停用逾期阻擋後，逾期罰款與特殊規則完全交由人工櫃檯處理，請確認館方流程已知悉。
- AR/AQ 規則依賴館端 SIP2 格式約定，若日後 SIP2 設定改變，可能需要再微調判斷條件。

回滾建議：
- 若遇到嚴重問題，可回滾到先前 v0.86.2beta 版本（請確認本地資料庫與設定檔備份）。

---
請於實機測試完成後回報，我可以再協助調整後續 0.9.x 小版本細節。