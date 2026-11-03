#!/usr/bin/env bash
# Source before running uv or Compose. Select app, benchmarks, or qwen environment.
export LANGUAGE_BOT_DATA_DIR="${LANGUAGE_BOT_DATA_DIR:-$HOME/language_bot_data}"
case "$LANGUAGE_BOT_DATA_DIR" in /*) ;; *) echo 'LANGUAGE_BOT_DATA_DIR must be absolute' >&2; return 1 2>/dev/null || exit 1;; esac
export UV_PROJECT_ENVIRONMENT="$LANGUAGE_BOT_DATA_DIR/venvs/${1:-app}"
export UV_CACHE_DIR="$LANGUAGE_BOT_DATA_DIR/cache/uv"
export UV_PYTHON_INSTALL_DIR="$LANGUAGE_BOT_DATA_DIR/cache/python"
export HF_HOME="$LANGUAGE_BOT_DATA_DIR/cache/huggingface"
export XDG_CACHE_HOME="$LANGUAGE_BOT_DATA_DIR/cache"
export TMPDIR="$LANGUAGE_BOT_DATA_DIR/tmp"
export PYTHONPYCACHEPREFIX="$LANGUAGE_BOT_DATA_DIR/cache/pycache"
export RUFF_CACHE_DIR="$LANGUAGE_BOT_DATA_DIR/cache/ruff"
export PYTEST_ADDOPTS="-o cache_dir=$LANGUAGE_BOT_DATA_DIR/cache/pytest"
mkdir -p "$LANGUAGE_BOT_DATA_DIR"/{models,benchmarks,venvs,cache,tmp,logs,backups}
