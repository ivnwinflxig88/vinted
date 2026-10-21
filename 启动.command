#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  printf '需要 Python 3。请从 https://www.python.org/downloads/macos/ 安装后再次双击。\n'
  read -r -p '按回车退出。' _reply
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  printf '需要 Python 3.9 或更新版本，请安装官网 macOS universal2 版本。\n'
  read -r -p '按回车退出。' _reply
  exit 1
fi
python3 adspower_manager.py
