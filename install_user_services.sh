#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${ROOT_DIR}/systemd/user"
DEST_DIR="${HOME}/.config/systemd/user"
REQ_FILE="${ROOT_DIR}/requirements.txt"

PY_SERVICE="pyserver.service"
BROWSER_SERVICE="kiosk-browser.service"

INSTALL_PY_DEPS=1
INSTALL_SERVICES=1

usage() {
  cat <<'EOF'
用法：
  ./install_user_services.sh [--services-only] [--python-only] [--skip-python-deps]

預設會同時安裝：
  - systemd user services
  - requirements.txt 內的 Python 套件
EOF
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

install_systemd_services() {
  if [[ ! -f "${SRC_DIR}/${PY_SERVICE}" ]]; then
    echo "[ERROR] 找不到 ${SRC_DIR}/${PY_SERVICE}"
    exit 1
  fi

  if [[ ! -f "${SRC_DIR}/${BROWSER_SERVICE}" ]]; then
    echo "[ERROR] 找不到 ${SRC_DIR}/${BROWSER_SERVICE}"
    exit 1
  fi

  mkdir -p "${DEST_DIR}"
  cp -f "${SRC_DIR}/${PY_SERVICE}" "${DEST_DIR}/${PY_SERVICE}"
  cp -f "${SRC_DIR}/${BROWSER_SERVICE}" "${DEST_DIR}/${BROWSER_SERVICE}"

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
