#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# Ubuntu 20.04 Kiosk Full Setup Script
# - GDM autologin kiosk (Xorg)
# - Kiosk X Session (matchbox) that runs Flask + Firefox --kiosk
# - Hardening: disable Ctrl+Alt+Del, tty2-tty6, sysrq, power keys
# - Copy project into /home/kiosk/下載/圖書館-前端
# - Optional ACL for ros to edit /home/kiosk via VS Code
# =========================================================

# ===== 可調參數 =====
TARGET_USER="kiosk"
SOURCE_PROJECT_DIR_DEFAULT="$(pwd)"
TARGET_PROJECT_DIR="/home/${TARGET_USER}/下載/圖書館-前端"

URL="http://127.0.0.1:5000/"
WAIT_TIMEOUT=60

BROWSER_BIN="firefox"
BROWSER_ARGS=(--kiosk)

GDM_CONF="/etc/gdm3/custom.conf"

# ===== 工具函數 =====
need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "[ERROR] 這支腳本要用 root 執行，請用：sudo bash $0"
    exit 1
  fi
}

log() { printf "\n\033[1m%s\033[0m\n" "$*"; }

ensure_pkg() {
  log "安裝必要套件（firefox/curl/matchbox/unclutter/xset/acl）"
  apt-get update -y
  apt-get install -y \
    firefox curl \
    matchbox-window-manager unclutter x11-xserver-utils \
    acl
}

ensure_user() {
  if ! id "${TARGET_USER}" &>/dev/null; then
    log "建立使用者 ${TARGET_USER}"
    # 建立但不設定密碼（給 GDM autologin 用）
    adduser --disabled-password --gecos "" "${TARGET_USER}"
  else
    log "使用者 ${TARGET_USER} 已存在"
  fi

  # kiosk 不該有 sudo 權限
  deluser "${TARGET_USER}" sudo 2>/dev/null || true

  # 本機免密碼（用於 GDM autologin / 本機登入）
  # 注意：SSH 通常不允許空密碼，所以 VS Code 建議用 ros 登入 SSH
  passwd -d "${TARGET_USER}" >/dev/null 2>&1 || true
}

set_gdm_autologin() {
  log "設定 GDM 自動登入：${TARGET_USER}（並強制 Xorg）"

  [[ -f "${GDM_CONF}" ]] || touch "${GDM_CONF}"

  # 確保有 [daemon]
  if ! grep -q '^\[daemon\]' "${GDM_CONF}"; then
    printf "[daemon]\n" | cat - "${GDM_CONF}" > "${GDM_CONF}.tmp" && mv "${GDM_CONF}.tmp" "${GDM_CONF}"
  fi

  # 移除舊設定（避免重複）
  sed -i \
    -e '/^[[:space:]]*WaylandEnable[[:space:]]*=/d' \
    -e '/^[[:space:]]*AutomaticLoginEnable[[:space:]]*=/d' \
    -e '/^[[:space:]]*AutomaticLogin[[:space:]]*=/d' \
    "${GDM_CONF}"

  # 插入新設定（不留空格，避免解析差異）
  awk -v user="${TARGET_USER}" '
    BEGIN{done=0}
    /^\[daemon\]$/ && done==0 {
      print $0
      print "WaylandEnable=false"
      print "AutomaticLoginEnable=True"
      print "AutomaticLogin=" user
      done=1
      next
    }
    {print $0}
  ' "${GDM_CONF}" > "${GDM_CONF}.tmp" && mv "${GDM_CONF}.tmp" "${GDM_CONF}"
}

copy_project() {
  local src="${1}"
  log "複製專案：${src} -> ${TARGET_PROJECT_DIR}"

  if [[ ! -d "${src}" ]]; then
    echo "[ERROR] 來源專案資料夾不存在：${src}"
    exit 1
  fi

  mkdir -p "/home/${TARGET_USER}/下載"
  rm -rf "${TARGET_PROJECT_DIR}"
  cp -a "${src}" "${TARGET_PROJECT_DIR}"
  chown -R "${TARGET_USER}:${TARGET_USER}" "/home/${TARGET_USER}/下載"
}

ensure_kiosk_session() {
  log "建立 Kiosk X Session（matchbox：不進 GNOME，只跑 Flask + Firefox）"

  # 1) 建立 xsessions entry
  cat > /usr/share/xsessions/kiosk.desktop <<'EOF'
[Desktop Entry]
Name=Kiosk
Comment=Locked kiosk session
Exec=/usr/local/bin/kiosk-session.sh
Type=Application
EOF

  # 2) 建立 session 腳本
  cat > /usr/local/bin/kiosk-session.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${TARGET_PROJECT_DIR}"
APP="\${PROJECT_DIR}/app.py"
URL="${URL}"
WAIT_TIMEOUT="${WAIT_TIMEOUT}"

# 關閉螢幕保護/省電
xset s off
xset -dpms
xset s noblank

# 隱藏滑鼠
unclutter -idle 0.1 -root &

# 極簡 WM（無面板、無系統選單）
matchbox-window-manager -use_titlebar no &

# Flask：掛了就重啟
server_loop() {
  while true; do
    cd "\${PROJECT_DIR}"
    /usr/bin/python3 "\${APP}" || true
    sleep 1
  done
}
server_loop &

# 等服務就緒
for i in \$(seq 1 "\${WAIT_TIMEOUT}"); do
  if curl -fsS "\${URL}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Firefox：被關掉/崩潰就立刻拉回來
while true; do
  ${BROWSER_BIN} ${BROWSER_ARGS[*]} "\${URL}" || true
  sleep 0.5
done
EOF

  chmod +x /usr/local/bin/kiosk-session.sh

  # 3) 讓 kiosk 使用者預設 Session=kiosk
  cat > "/home/${TARGET_USER}/.dmrc" <<'EOF'
[Desktop]
Session=kiosk
EOF
  chown "${TARGET_USER}:${TARGET_USER}" "/home/${TARGET_USER}/.dmrc"
  chmod 644 "/home/${TARGET_USER}/.dmrc"
}

harden_lockdown() {
  log "加固鎖定：禁 Ctrl+Alt+Del / 禁 tty2~tty6 / 禁 SysRq / 電源鍵忽略"

  # Ctrl+Alt+Del
  systemctl mask ctrl-alt-del.target >/dev/null 2>&1 || true
  systemctl daemon-reload >/dev/null 2>&1 || true

  # 禁 tty2~tty6（保留 tty1 方便救援）
  systemctl mask \
    getty@tty2.service getty@tty3.service getty@tty4.service \
    getty@tty5.service getty@tty6.service >/dev/null 2>&1 || true

  # 禁 SysRq（避免 REISUB）
  echo 'kernel.sysrq = 0' > /etc/sysctl.d/99-kiosk.conf
  sysctl --system >/dev/null 2>&1 || true

  # 電源鍵/睡眠鍵忽略（避免跳出關機 UI）
  sed -i '/^HandlePowerKey=/d;/^HandleRebootKey=/d;/^HandleSuspendKey=/d;/^HandleHibernateKey=/d;/^HandleLidSwitch=/d' /etc/systemd/logind.conf
  cat >> /etc/systemd/logind.conf <<'EOF'

HandlePowerKey=ignore
HandleRebootKey=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
EOF
  systemctl restart systemd-logind >/dev/null 2>&1 || true
}

optional_acl_for_ros() {
  # 讓 ros 用 VS Code 直接改 /home/kiosk，不用一直 sudo
  if id ros &>/dev/null; then
    log "加上 ACL：讓 ros 可直接讀寫 /home/${TARGET_USER}（方便 VS Code Remote SSH）"
    setfacl -R -m u:ros:rwx "/home/${TARGET_USER}" || true
    setfacl -R -d -m u:ros:rwx "/home/${TARGET_USER}" || true
  fi
}

print_summary() {
  log "完成！重點設定如下"
  echo "- Ubuntu: 20.04.x（已用 matchbox 建立 Kiosk Session）"
  echo "- GDM autologin: ${TARGET_USER}"
  echo "- Project dir: ${TARGET_PROJECT_DIR}"
  echo "- URL: ${URL}"
  echo ""
  echo "下一步："
  echo "  sudo reboot"
  echo ""
  echo "若要維修（建議用 ros SSH / VS Code）："
  echo "  - 專案路徑：${TARGET_PROJECT_DIR}"
  echo "  - kiosk session 腳本：/usr/local/bin/kiosk-session.sh"
  echo "  - 如需暫時解除 tty 封鎖：sudo systemctl unmask getty@tty2.service ...（或進救援模式）"
}

main() {
  need_root

  local src="${1:-${SOURCE_PROJECT_DIR_DEFAULT}}"

  ensure_pkg
  ensure_user
  set_gdm_autologin
  copy_project "${src}"
  ensure_kiosk_session
  harden_lockdown
  optional_acl_for_ros
  print_summary
}

main "$@"
