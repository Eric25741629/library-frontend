#!/usr/bin/env bash

# install_services.sh

#

# 功能：

#  1) systemd --user 啟動 Flask 伺服器：python3 ~/下載/圖書館-前端/app.py

#  2) 等 http://127.0.0.1:5000/ 可連線後，開 Firefox 到該網址

#  3) Firefox 退出/崩潰後自動重啟（Restart=always）

#

# 安裝：  ./install_services.sh install

# 解除：  ./install_services.sh uninstall

# 日誌：  journalctl --user -u pyserver.service -f

#        journalctl --user -u kiosk-browser.service -f



set -euo pipefail



# ====== 你的專案設定（已依你貼的內容填好）======

PROJECT_DIR="$HOME/下載/圖書館-前端"

SERVER_CMD="/usr/bin/python3 $HOME/下載/圖書館-前端/app.py"

URL="http://127.0.0.1:5000/"

WAIT_TIMEOUT=60



BROWSER_BIN="firefox"

BROWSER_ARGS=(--new-window)

# 若你的 Firefox 支援 kiosk，可改成：BROWSER_ARGS=(--kiosk)



# ====== 安裝位置 ======

SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

BIN_DIR="$HOME/bin"

WAIT_SCRIPT="$BIN_DIR/start_browser_after_server.sh"

PY_UNIT="$SYSTEMD_USER_DIR/pyserver.service"

BROWSER_UNIT="$SYSTEMD_USER_DIR/kiosk-browser.service"



log() { printf "\n\033[1m%s\033[0m\n" "$*"; }



sanity_checks() {

  [[ -d "$PROJECT_DIR" ]] || { echo "[ERROR] 專案資料夾不存在：$PROJECT_DIR" >&2; exit 1; }

  command -v systemctl >/dev/null 2>&1 || { echo "[ERROR] 找不到 systemctl（需要 systemd）。" >&2; exit 1; }

  command -v python3  >/dev/null 2>&1 || { echo "[ERROR] 找不到 python3。" >&2; exit 1; }

  command -v "$BROWSER_BIN" >/dev/null 2>&1 || { echo "[WARN] 找不到 $BROWSER_BIN，請先安裝 Firefox。" >&2; }

}



write_wait_script() {

  mkdir -p "$BIN_DIR"



  # 直接把變數寫進檔案（不使用 sed 替換，避免你遇到的錯誤）

  cat > "$WAIT_SCRIPT" <<EOF

#!/usr/bin/env bash

set -euo pipefail



URL="$URL"

WAIT_TIMEOUT="$WAIT_TIMEOUT"

BROWSER_BIN="$BROWSER_BIN"

BROWSER_ARGS=($(printf '%q ' "${BROWSER_ARGS[@]}"))



have_cmd() { command -v "\$1" >/dev/null 2>&1; }



wait_for_http() {

  local url="\$1"

  local timeout="\$2"



  if have_cmd curl; then

    for i in \$(seq 1 "\$timeout"); do

      if curl -fsS "\$url" >/dev/null 2>&1; then

        return 0

      fi

      sleep 1

    done

    return 1

  fi



  if have_cmd python3; then

    python3 - <<PY || return 1

import sys, time, socket, urllib.parse

u = urllib.parse.urlparse("$URL")

host = u.hostname or "127.0.0.1"

port = u.port or (443 if u.scheme=="https" else 80)

path = u.path or "/"

timeout = int("$WAIT_TIMEOUT")

for _ in range(timeout):

    try:

        s = socket.create_connection((host, port), timeout=2)

        req = f"GET {path} HTTP/1.1\\r\\nHost: {host}\\r\\nConnection: close\\r\\n\\r\\n"

        s.sendall(req.encode("utf-8"))

        s.recv(1)

        s.close()

        sys.exit(0)

    except Exception:

        time.sleep(1)

sys.exit(1)

PY

    return 0

  fi



  echo "[ERROR] 需要 curl 或 python3 才能等待服務就緒" >&2

  return 1

}



echo "[INFO] Waiting for server: \$URL (timeout: \${WAIT_TIMEOUT}s)"

if ! wait_for_http "\$URL" "\$WAIT_TIMEOUT"; then

  echo "[ERROR] Timeout waiting for \$URL" >&2

  exit 1

fi



echo "[INFO] Server is up. Launching browser..."

exec "\$BROWSER_BIN" "\${BROWSER_ARGS[@]}" "\$URL"

EOF



  chmod +x "$WAIT_SCRIPT"

}



write_py_unit() {

  mkdir -p "$SYSTEMD_USER_DIR"

  cat > "$PY_UNIT" <<EOF

[Unit]

Description=Python Server (Flask) - autostart

After=network-online.target

Wants=network-online.target



[Service]

Type=simple

WorkingDirectory=$PROJECT_DIR

ExecStart=$SERVER_CMD

Restart=on-failure

RestartSec=2



[Install]

WantedBy=default.target

EOF

}



write_browser_unit() {

  mkdir -p "$SYSTEMD_USER_DIR"

  cat > "$BROWSER_UNIT" <<EOF

[Unit]

Description=Firefox Auto (restart on crash)

Wants=pyserver.service

After=pyserver.service



[Service]

Type=simple

ExecStart=$WAIT_SCRIPT

Restart=always

RestartSec=2



[Install]

WantedBy=default.target

EOF

}



install() {

  log "1) 檢查環境"

  sanity_checks



  log "2) 產生等待腳本（等伺服器就緒後開 Firefox）"

  write_wait_script

  echo "   - $WAIT_SCRIPT"



  log "3) 產生 systemd --user 服務檔"

  write_py_unit

  write_browser_unit

  echo "   - $PY_UNIT"

  echo "   - $BROWSER_UNIT"



  log "4) 註冊 + 啟用 + 立刻啟動（你要的『全部』操作都在這裡）"

  systemctl --user daemon-reload

  systemctl --user enable --now pyserver.service

  systemctl --user enable --now kiosk-browser.service



  log "完成！常用指令"

  echo "狀態：systemctl --user status pyserver.service kiosk-browser.service"

  echo "日誌：journalctl --user -u pyserver.service -f"

  echo "日誌：journalctl --user -u kiosk-browser.service -f"

}



uninstall() {

  log "停止並停用"

  systemctl --user disable --now kiosk-browser.service 2>/dev/null || true

  systemctl --user disable --now pyserver.service 2>/dev/null || true



  log "移除檔案"

  rm -f "$BROWSER_UNIT" "$PY_UNIT" "$WAIT_SCRIPT" || true



  log "重新載入"

  systemctl --user daemon-reload



  log "已解除"

}



case "${1:-}" in

  install) install ;;

  uninstall) uninstall ;;

  *)

    echo "用法：$0 {install|uninstall}"

    exit 2

    ;;

esac

