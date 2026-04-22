#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${ROOT_DIR}/systemd/user"
DEST_DIR="${HOME}/.config/systemd/user"
USER_HOME="${HOME}"
HELPER_DIR="${USER_HOME}/bin"
HELPER_SCRIPT="${HELPER_DIR}/start_browser_after_server.sh"
WAIT_GUI_SCRIPT="${HELPER_DIR}/wait_gui_ready.sh"

is_project_dir() {
  local dir="$1"
  [[ -f "${dir}/app.py" && -f "${dir}/requirements.txt" && -f "${dir}/install_user_services.sh" ]]
}

get_origin_url() {
  local dir="$1"
  git -C "${dir}" config --get remote.origin.url 2>/dev/null || true
}

resolve_project_dir() {
  local root_dir="$1"
  local user_home="$2"
  local project_name=""
  local root_origin=""
  local candidate_origin=""
  local first_valid=""
  local candidate=""
  local script_path=""
  local -a candidates=()

  if [[ "${root_dir}" == "${user_home}"/* ]] && is_project_dir "${root_dir}"; then
    echo "${root_dir}"
    return 0
  fi

  project_name="$(basename "${root_dir}")"
  root_origin="$(get_origin_url "${root_dir}")"

  # Common local checkout paths under the target user's home.
  candidates+=(
    "${user_home}/下載/${project_name}"
    "${user_home}/${project_name}"
    "${user_home}/lib/${project_name}"
    "${user_home}/lib/library"
    "${user_home}/workspace/${project_name}"
    "${user_home}/projects/${project_name}"
  )

  for candidate in "${candidates[@]}"; do
    if ! is_project_dir "${candidate}"; then
      continue
    fi

    if [[ -z "${first_valid}" ]]; then
      first_valid="${candidate}"
    fi

    if [[ -n "${root_origin}" ]]; then
      candidate_origin="$(get_origin_url "${candidate}")"
      if [[ "${candidate_origin}" == "${root_origin}" ]]; then
        echo "${candidate}"
        return 0
      fi
    fi
  done

  # Fallback: scan home for matching install scripts and prefer same git origin.
  while IFS= read -r script_path; do
    candidate="$(dirname "${script_path}")"
    if ! is_project_dir "${candidate}"; then
      continue
    fi

    if [[ -z "${first_valid}" ]]; then
      first_valid="${candidate}"
    fi

    if [[ -n "${root_origin}" ]]; then
      candidate_origin="$(get_origin_url "${candidate}")"
      if [[ "${candidate_origin}" == "${root_origin}" ]]; then
        echo "${candidate}"
        return 0
      fi
    fi
  done < <(find "${user_home}" -maxdepth 4 -type f -name install_user_services.sh 2>/dev/null)

  if [[ -n "${first_valid}" ]]; then
    echo "${first_valid}"
    return 0
  fi

  if is_project_dir "${root_dir}"; then
    echo "${root_dir}"
    return 0
  fi

  echo "${root_dir}"
}

resolve_log_dir() {
  local project_dir="$1"
  local user_home="$2"
  local fallback_log_dir="${user_home}/lib/library/logs"

  if [[ "${project_dir}" == "${user_home}"/* ]] && is_project_dir "${project_dir}"; then
    echo "${project_dir}/logs"
    return 0
  fi

  mkdir -p "${fallback_log_dir}"
  echo "${fallback_log_dir}"
}

PROJECT_DIR="${PROJECT_DIR_OVERRIDE:-$(resolve_project_dir "${ROOT_DIR}" "${USER_HOME}")}"
REQ_FILE="${PROJECT_DIR}/requirements.txt"
LOG_DIR="${LOG_DIR_OVERRIDE:-$(resolve_log_dir "${PROJECT_DIR}" "${USER_HOME}")}"

PY_SERVICE="pyserver.service"
BROWSER_SERVICE="kiosk-browser.service"

INSTALL_PY_DEPS=1
INSTALL_SERVICES=1

usage() {
  cat <<'EOF'
用法：
  ./install_user_services.sh [--services-only] [--python-only] [--skip-python-deps]

可選環境變數：
  PROJECT_DIR_OVERRIDE=/path/to/project  強制指定 service 使用的專案目錄
  LOG_DIR_OVERRIDE=/path/to/logs         強制指定後端 logs 目錄

預設會同時安裝：
  - systemd user services
  - requirements.txt 內的 Python 套件
EOF
}

print_user_bus_reminder() {
  cat <<EOF
[REMINDER]
systemctl --user 需要該使用者的登入 session / user bus。
如果你是在沒有 GUI 登入、沒有 SSH 持續 session、或剛切換使用者後直接執行，
就可能出現：Failed to connect to bus: No such file or directory

建議做法：
  1. 以目標使用者登入後再執行本腳本
  2. 或先啟用 linger：sudo loginctl enable-linger $(id -un)
  3. 若只是要安裝 Python 套件，可使用 --python-only

EOF
}

check_user_bus() {
  if systemctl --user show-environment >/dev/null 2>&1; then
    return 0
  fi

  print_user_bus_reminder
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --services-only)
      INSTALL_PY_DEPS=0
      INSTALL_SERVICES=1
      ;;
    --python-only)
      INSTALL_SERVICES=0
      INSTALL_PY_DEPS=1
      ;;
    --skip-python-deps)
      INSTALL_PY_DEPS=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] 不支援的參數：$1"
      usage
      exit 1
      ;;
  esac
  shift
done

install_python_deps() {
  if [[ ! -f "${REQ_FILE}" ]]; then
    echo "[WARN] 找不到 ${REQ_FILE}，略過 Python 依賴安裝"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] 找不到 python3，無法安裝 Python 依賴"
    exit 1
  fi

  if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "[ERROR] python3 -m pip 不可用，無法安裝 Python 依賴"
    exit 1
  fi

  echo "[INFO] 安裝 Python 依賴：${REQ_FILE}"
  python3 -m pip install --user -r "${REQ_FILE}"
}

render_template() {
  local src_file="$1"
  local dest_file="$2"

  python3 - "$src_file" "$dest_file" "$PROJECT_DIR" "$USER_HOME" "$LOG_DIR" <<'PY'
from pathlib import Path
import sys

src = Path(sys.argv[1])
dest = Path(sys.argv[2])
project_dir = sys.argv[3]
user_home = sys.argv[4]
log_dir = sys.argv[5]

text = src.read_text(encoding='utf-8')
text = text.replace('__PROJECT_DIR__', project_dir)
text = text.replace('__HOME__', user_home)
text = text.replace('__LOG_DIR__', log_dir)
dest.write_text(text, encoding='utf-8')
PY
}

install_browser_helper() {
  mkdir -p "${HELPER_DIR}"
  cat > "${WAIT_GUI_SCRIPT}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

DISPLAY_VALUE="${DISPLAY:-:0}"
TIMEOUT="${1:-180}"
DISPLAY_NUM="${DISPLAY_VALUE#:}"

is_ready() {
  [[ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]
}

for _ in $(seq 1 "${TIMEOUT}"); do
  if is_ready; then
    echo "[INFO] GUI ready on DISPLAY=${DISPLAY_VALUE}"
    exit 0
  fi
  sleep 1
done

echo "[WARN] GUI not ready after ${TIMEOUT}s (DISPLAY=${DISPLAY_VALUE})"
exit 1
EOF
  chmod +x "${WAIT_GUI_SCRIPT}"

  cat > "${HELPER_SCRIPT}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

URL="http://127.0.0.1:5000/"
BROWSER_BIN="${BROWSER_BIN:-firefox}"
export DISPLAY="${DISPLAY:-:0}"

# 等 X display 就緒，再給 GUI session 一點時間穩定。
"${HOME}/bin/wait_gui_ready.sh" 180 || true
sleep 10

# 等 Flask server 起來。
for _ in $(seq 1 60); do
  curl -fsS "${URL}" >/dev/null 2>&1 && break
  sleep 1
done

# 離開全螢幕的監控由前端 JS 負責（/api/frontend/fullscreen_lost）。
# 這裡只負責啟動 Firefox，若它退出就再啟動。systemd Restart=always 兜底。
while true; do
  "${BROWSER_BIN}" --kiosk "${URL}" || true
  sleep 2
done
EOF
  chmod +x "${HELPER_SCRIPT}"
}

install_firefox_userjs() {
  # 找 Firefox default-release profile 並寫入 user.js，
  # 避免崩潰後進入 Safe Mode 或顯示 session 還原對話框。
  local profile_dir=""
  local ff_base="${HOME}/.mozilla/firefox"

  if [[ ! -d "${ff_base}" ]]; then
    echo "[INFO] Firefox profile 目錄不存在，略過 user.js 安裝（Firefox 首次啟動後會自動建立）"
    return 0
  fi

  # 優先選 default-release，其次選任意 profile
  profile_dir="$(find "${ff_base}" -maxdepth 1 -type d \( -name "*.default-release" -o -name "*.default-release-*" \) | head -1)"
  if [[ -z "${profile_dir}" ]]; then
    profile_dir="$(find "${ff_base}" -maxdepth 1 -type d -name "*.default" | head -1)"
  fi

  if [[ -z "${profile_dir}" ]]; then
    echo "[INFO] 找不到 Firefox profile，略過 user.js 安裝"
    return 0
  fi

  cat > "${profile_dir}/user.js" << 'USERJS'
// Kiosk: never enter safe mode after crashes
user_pref("toolkit.startup.max_resumed_crashes", -1);
user_pref("toolkit.startup.recent_crashes", 0);
// Kiosk: no session restore / crash recovery prompt
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.sessionstore.max_resumed_crashes", -1);
// Kiosk: no default browser check or update prompts
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("app.update.auto", false);
user_pref("browser.crashReports.unsubmittedCheck.autoSubmit2", false);
USERJS
  echo "[INFO] Firefox user.js 已寫入：${profile_dir}/user.js"
}

install_systemd_services() {
  if [[ ! -f "${SRC_DIR}/${PY_SERVICE}" ]]; then
    echo "[ERROR] 找不到 ${SRC_DIR}/${PY_SERVICE}"
    exit 1
  fi

  if [[ ! -f "${SRC_DIR}/${BROWSER_SERVICE}" ]]; then
    echo "[ERROR] 找不到 ${SRC_DIR}/${BROWSER_SERVICE}"
    exit 1
  fi

  if ! check_user_bus; then
    echo "[WARN] 偵測不到可用的 systemd user bus，略過服務安裝。"
    echo "[WARN] 你可以先登入目標使用者再重跑，或執行：sudo loginctl enable-linger $(id -un)"
    return 1
  fi

  echo "[INFO] 服務將使用專案路徑：${PROJECT_DIR}"
  echo "[INFO] 後端 logs 路徑：${LOG_DIR}"
  if [[ "${PROJECT_DIR}" == "${ROOT_DIR}" && "${ROOT_DIR}" != "${USER_HOME}"/* ]]; then
    echo "[WARN] 目前是從其他使用者的目錄安裝，未找到 ${USER_HOME} 下的同名專案副本。"
    echo "[WARN] 如需強制指定，請設定 PROJECT_DIR_OVERRIDE=/path/to/project"
  fi

  mkdir -p "${DEST_DIR}"
  install_browser_helper
  install_firefox_userjs
  render_template "${SRC_DIR}/${PY_SERVICE}" "${DEST_DIR}/${PY_SERVICE}"
  render_template "${SRC_DIR}/${BROWSER_SERVICE}" "${DEST_DIR}/${BROWSER_SERVICE}"

  systemctl --user daemon-reload
  systemctl --user enable --now "${PY_SERVICE}" "${BROWSER_SERVICE}"

  echo "已安裝並啟用："
  echo "  - ${PY_SERVICE}"
  echo "  - ${BROWSER_SERVICE}"
  echo ""
  echo "目前狀態："
  systemctl --user --no-pager status "${PY_SERVICE}" "${BROWSER_SERVICE}" || true
}

if [[ "${INSTALL_PY_DEPS}" -eq 1 ]]; then
  install_python_deps
fi

if [[ "${INSTALL_SERVICES}" -eq 1 ]]; then
  install_systemd_services
fi
