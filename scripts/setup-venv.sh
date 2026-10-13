#!/bin/bash
# 幂等确保 pdf-translate 的 Python venv 可用(由 systemd ExecStartPre 调用)
set -u
VENV=/opt/pdf-translate/venv

if [ -x "$VENV/bin/python" ] \
   && "$VENV/bin/python" -c "import fastapi, uvicorn, babeldoc" 2>/dev/null \
   && "$VENV/bin/python" -c "import bitstring; assert bitstring.__version__.startswith('4.')" 2>/dev/null; then
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[setup-venv] 缺少 uv。请先执行: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "[setup-venv] 或手动创建 venv 后重试: systemctl restart pdf-translate" >&2
  exit 1
fi

echo "[setup-venv] 正在创建/补全 $VENV …"
uv venv -p 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" fastapi "uvicorn[standard]" python-multipart
uv pip install --python "$VENV/bin/python" babeldoc
uv pip install --python "$VENV/bin/python" "bitstring==4.2.3"
echo "[setup-venv] 完成"
