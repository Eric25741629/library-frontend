# Release: 1.0.0 兩階段 UART 協定 + 還書流程嚴格化

發布日期: 2026-05-13

狀態: Verified on /dev/uno @ 9600

## 重點說明

### 1. 兩階段 UART 協定（規格 `說明書_還書步驟_2025-12-07_2300.pdf`）

舊版把韌體立即回的 `ack` 當成完成訊息，導致 SIP2 已歸還但機器可能沒真的把書放進分類箱。
重寫 `machine_controller.py`：

- 動作指令兩階段：Phase 1 等 `ack` / `ack-busy` / `error state X`，Phase 2 等對應完成訊息
  （`device power on` / `homed` / `opened` / `closed` / `been put1` / `canceled` …）
- 查詢指令 (`state`/`act`/`bookok` 等) 不進 Phase 2，立即回單行結果
- 新增 helper：`is_completion(cmd, resp)` / `is_busy_response(resp)` / `is_error_state_response(resp)` —
  全 routes 已改用 helper，**不再使用 `"ack" in resp` 字串比對**

### 2. /dev/uno 實機驗證（2026-05-13）

| 指令 | 實測 Phase 1 | 實測 Phase 2 | 延遲 |
|------|--------------|--------------|------|
| `dep1` (state 5→2) | `ack` | `device power on` | ~0s |
| `cancel` (state 3→2) | `ack` (0.22s) | `canceled` | **~12s** |

舊版前端註解 (`return_book.html:1080`) 寫「cancel 韌體只送 ack」**經實測確認為錯誤**，
cancel 跟其他指令一樣是兩階段。`ACTION_TIMEOUTS["cancel"]` 從 15s 提高到 30s。

韌體 idle 時每 ~11 秒會週期性廣播當前 state digit，`_apply_response_side_effects`
已正確處理 digit 同步旗標。

### 3. wake_up / check_idle 優化（與規格 §3 / §8 對齊）

- `wake_up()`：實際等到 dep1 完成訊息才回；依「device power on, not homed」決定
  `is_homed`；若意外進到 state 1 自動補 `homing`，post-condition 保證可立即送下個動作
- `check_idle()`：先查 state 再決策，省下舊版盲目 homing 的 10-20s
  - state 2 (homed)：直接 `dep0` ✓
  - state 3 (opened)：先 `cancel` 再 `dep0`
  - state 4 (closed)：規格不允許 cancel，改 `homing` 再 `dep0`

### 4. /api/return 流程嚴格化

舊版在機器 `state ≠ 3` 時盲送 homing fallback，後續 close 會在錯誤狀態下失敗，
但流程仍繼續到 SIP2 checkin，造成「圖書館已歸還、box_inventory 已寫入、書本實際沒進機器」的資料錯位。改成：

- **`state ≠ 3` 嚴格拒絕**：回 `MACHINE_NOT_OPEN` (409)，前端引導使用者重來
- **`close_door()` 失敗**：回 `MACHINE_CLOSE_FAILED` (500) + 非同步 reopen，**不打 SIP2**
- **`sort_book()` 失敗**：SIP2 已 commit 無法 reverse，但 response 帶 `sort_warning.code='SORT_FAILED'`，
  前端顯示 sticky modal 提示「請洽櫃台」，後端 log ERROR 給管理員
- **`status` 讀取例外**：回 `MACHINE_STATUS_UNKNOWN` (503)，不再盲送 homing

### 5. 前端對應新錯誤碼

`handleReturnFailureCode` 新增分類：
- `MACHINE_NOT_OPEN` → 8 秒 notice modal
- `MACHINE_CLOSE_FAILED` → 10 秒 error modal
- `MACHINE_STATUS_UNKNOWN` → sticky modal（櫃台介入）

三個還書入口 (`finalizeReturn` / `finishBookAndStartAttachment` / `finalizeAttachmentReturn`)
都加上 `sort_warning` 處理（優先級高於 overdue 訊息）。

### 6. 還書進度（取代固定秒數）

前端從固定 setTimeout 14s/17s 改成輪詢 `/api/return_progress` 取得後端實際階段：
`pre_check → closing → closed → checkin → sorting → done`。Stage 顯示真實進度，
不再「字幕跑完了機器還沒做完」。

### 7. /api/cancel 與相關 endpoint 全面改用 helper

- `/api/cancel`：依完成 / busy / error state / 未確認回 200 / 409 / 400 / 500
- `/api/hardware/close`：同上（透過 cancel）
- `/api/admin/machine/reset|force_homing|clear_error`：homing/dep1 都用 `is_completion`

### 8. 機器狀態語意精度

`_collect_machine_state` 改用 state digit 直接推：state 3 → `opened`、state 4 → `closed`，
不再把 state 2/3/4 都壓成 `homed`。UI 與 watchdog 都看得到正確語意。

## 測試

- pytest 套件 108 cases（原 105 + 新增 3）：覆蓋 close 失敗、sort 失敗 success-with-warning、
  state≠3 嚴格拒絕；`test_ack_alone_is_not_success` 取代舊的「ack 也算成功」測試
- mocked 兩階段協定 11 情境
- mocked wake_up / check_idle 6 情境（A-F）
- **實機 E2E**：/dev/uno @ 9600 跑過 state / dep1 / cancel 兩階段完整流程

## 已知限制

- `RELEASE_NOTES.md` 與 README 內部分指令範例的 baudrate 顯示為 38400 / 19200；
  實機 `machine_config.json` 為 9600。`shared.py` 預設提高到 19200（與規格 PDF 一致）但
  使用者的本機設定仍以 `machine_config.json` 為準
- watchdog `_watchdog_recover_machine` 仍 fire-and-forget 不檢查完成訊息
  （刻意保留 — recovery 階段機器狀態未知，硬性檢查反而易誤判失敗）

---

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