#!/usr/bin/env bash
# upload-macos-x64.sh — 在 Intel macOS 上本地构建 x64 二进制并上传到 GitHub Release
#
# 用途:
#   GitHub Actions 的免费 Intel macOS runner (macos-13) 严重限流,经常排队数小时
#   甚至超时。本脚本在开发者本机的 Intel Mac 上构建原生 x64 二进制,然后上传
#   到指定 tag 的 GitHub Release,并刷新 SHA256SUMS。
#
# 前置条件:
#   - 必须在 Intel (x86_64) macOS 上运行。脚本会校验架构,arm64 机器直接报错退出。
#   - 已安装 uv (https://docs.astral.sh/uv/)。
#   - 已设置 GH_TOKEN 环境变量为一个有 repo 权限的 GitHub Personal Access Token。
#     (用完即可撤销,脚本不保存 token。)
#
# 用法:
#   GH_TOKEN=ghp_xxx ./scripts/upload-macos-x64.sh v0.1.0
#   GH_TOKEN=ghp_xxx ./scripts/upload-macos-x64.sh v0.1.0 --skip-build
#       (--skip-build: 跳过构建,直接上传已有的 pyinstaller/dist/pycharm-sonar-mcp-macos-x64)
#
# 退出码: 0 成功; 非 0 失败(见各步骤)。
# 兼容 macOS 系统 Bash 3.2。

set -euo pipefail

PROG="pycharm-sonar-mcp"
ASSET_NAME="pycharm-sonar-mcp-macos-x64"
REPO="yingsf/pycharm-sonar-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$REPO_ROOT/pyinstaller/dist"

log()  { printf '%s\n' "$*"; }
err()  { printf 'error: %s\n' "$*" >&2; }

# --- 参数 ---
TAG="${1:-}"
SKIP_BUILD=0
if [ "${2:-}" = "--skip-build" ]; then SKIP_BUILD=1; fi

if [ -z "$TAG" ]; then
  err "用法: GH_TOKEN=ghp_xxx $0 <tag> [--skip-build]"
  err "示例: GH_TOKEN=ghp_xxx $0 v0.1.0"
  exit 2
fi

# --- token 校验 ---
if [ -z "${GH_TOKEN:-}" ]; then
  err "未设置 GH_TOKEN 环境变量。"
  err "请创建一个有 repo 权限的 token: https://github.com/settings/tokens"
  err "然后: GH_TOKEN=ghp_xxx $0 $TAG"
  exit 2
fi

# --- 架构校验:必须是 Intel x86_64 ---
ARCH="$(uname -m)"
if [ "$ARCH" != "x86_64" ]; then
  err "本脚本必须在 Intel (x86_64) macOS 上运行。当前架构: $ARCH"
  err "请在 Intel Mac 上执行; arm64 Mac 请用 CI 产出的 pycharm-sonar-mcp-macos-arm64。"
  exit 1
fi

if [ "$(uname)" != "Darwin" ]; then
  err "本脚本仅用于 macOS。当前系统: $(uname)"
  exit 1
fi

# --- 校验 tag 对应的 Release 存在 ---
log "校验 Release $TAG 是否存在..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/$REPO/releases/tags/$TAG")
if [ "$HTTP_CODE" != "200" ]; then
  err "Release $TAG 不存在 (HTTP $HTTP_CODE)。请先打 tag 触发 CI 创建 Release。"
  exit 1
fi

# --- 构建 ---
BINARY_PATH="$DIST_DIR/$ASSET_NAME"
if [ "$SKIP_BUILD" = "1" ]; then
  log "跳过构建 (--skip-build)。"
  if [ ! -f "$BINARY_PATH" ]; then
    err "二进制不存在: $BINARY_PATH。请去掉 --skip-build 先构建。"
    exit 1
  fi
else
  log "在 $REPO_ROOT 构建原生 x64 二进制..."
  cd "$REPO_ROOT"
  if ! command -v uv >/dev/null 2>&1; then
    err "未找到 uv。请先安装: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
  uv sync --python 3.12
  rm -rf "$DIST_DIR"
  # 用 --with 临时引入 pyinstaller,避免 uv pip install 装到与 uv run 不同的环境。
  uv run --with pyinstaller pyinstaller --noconfirm --clean --distpath "$DIST_DIR" pyinstaller/pycharm-sonar-mcp.spec
  if [ ! -f "$DIST_DIR/$PROG" ]; then
    err "构建失败: 未找到 $DIST_DIR/$PROG"
    exit 1
  fi
  mv "$DIST_DIR/$PROG" "$BINARY_PATH"
fi

# --- 冒烟测试 ---
log "校验二进制架构与冒烟测试..."
FILE_OUT="$(file "$BINARY_PATH")"
case "$FILE_OUT" in
  *x86_64*) log "架构: x86_64 ✓" ;;
  *) err "二进制不是 x86_64: $FILE_OUT"; exit 1 ;;
esac
"$BINARY_PATH" --version
"$BINARY_PATH" doctor || log "warn: doctor 报告问题 (exit $?)。"

# --- 上传到 Release ---
log "上传 $ASSET_NAME 到 Release $TAG..."
REL_JSON="/tmp/.psm_rel.json"
curl -s -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/$REPO/releases/tags/$TAG" -o "$REL_JSON"

# 用 python3 解析 JSON,避免 grep/sed 在含特殊字符的字段上出错。
UPLOAD_URL=$(python3 -c "import json;print(json.load(open('$REL_JSON'))['upload_url'].replace('{?name,label}',''))")
if [ -z "$UPLOAD_URL" ]; then
  err "无法获取 upload_url。"
  exit 1
fi

# 若已存在同名 asset,先删除(支持重复执行)。
ASSET_ID=$(python3 -c "
import json
r = json.load(open('$REL_JSON'))
for a in r.get('assets', []):
    if a['name'] == '$ASSET_NAME':
        print(a['id']); break
" || true)
if [ -n "$ASSET_ID" ]; then
  log "已存在同名 asset (id=$ASSET_ID),先删除..."
  curl -s -X DELETE -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$REPO/releases/assets/$ASSET_ID" -o /dev/null -w "delete: HTTP %{http_code}\n"
fi

UPLOAD_HTTP=$(curl -s -o /tmp/.psm_upload_resp -w "%{http_code}" \
  -X POST -H "Authorization: token $GH_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"$BINARY_PATH" \
  "$UPLOAD_URL?name=$ASSET_NAME")
if [ "$UPLOAD_HTTP" != "201" ]; then
  err "上传失败 (HTTP $UPLOAD_HTTP): $(head -c 300 /tmp/.psm_upload_resp)"
  exit 1
fi
SHA=$(shasum -a 256 "$BINARY_PATH" | awk '{print $1}')
log "上传成功。SHA-256: $SHA"

# --- 刷新 SHA256SUMS ---
log "刷新 SHA256SUMS..."
curl -sL -H "Authorization: token $GH_TOKEN" \
  "https://github.com/$REPO/releases/download/$TAG/SHA256SUMS" -o /tmp/.psm_sums || true
# 移除旧的 Intel 条目(若有),再追加新的,保持每行唯一。
if [ -s /tmp/.psm_sums ]; then
  grep -v "  $ASSET_NAME\$" /tmp/.psm_sums > /tmp/.psm_sums.new || true
else
  : > /tmp/.psm_sums.new
fi
echo "$SHA  $ASSET_NAME" >> /tmp/.psm_sums.new

SUMS_ASSET_ID=$(curl -s -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/$REPO/releases/tags/$TAG" -o "$REL_JSON" \
  && python3 -c "
import json
r = json.load(open('$REL_JSON'))
for a in r.get('assets', []):
    if a['name'] == 'SHA256SUMS':
        print(a['id']); break
" || true)
if [ -n "$SUMS_ASSET_ID" ]; then
  curl -s -X DELETE -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$REPO/releases/assets/$SUMS_ASSET_ID" -o /dev/null
fi
curl -s -o /dev/null -w "SHA256SUMS 上传: HTTP %{http_code}\n" \
  -X POST -H "Authorization: token $GH_TOKEN" \
  -H "Content-Type: text/plain" \
  --data-binary @/tmp/.psm_sums.new \
  "$UPLOAD_URL?name=SHA256SUMS"

rm -f /tmp/.psm_upload_resp /tmp/.psm_sums /tmp/.psm_sums.new /tmp/.psm_rel.json

log ""
log "完成。Release $TAG 现已包含 ${ASSET_NAME}。"
log "  https://github.com/$REPO/releases/tag/$TAG"
