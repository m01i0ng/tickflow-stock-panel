# 多阶段构建:前端 dist 拷进后端镜像,单容器运行
# 可选:构建网络无法直连官方源时,传入 --build-arg USE_CN_MIRROR=1 启用国内镜像
# 可选:stock-sdk 插件默认不打包(它抓取第三方财经网站接口,存在版权与反爬风险)。
#       如确需启用,传入 --build-arg INCLUDE_STOCKSDK=1 显式开启,使用风险自负。
ARG USE_CN_MIRROR=1
ARG INCLUDE_STOCKSDK=0
# 自选截图 OCR 是边缘功能: 默认仍安装保持兼容, 镜像瘦身可传 --build-arg INCLUDE_OCR=0
ARG INCLUDE_OCR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# 备用 PyPI 源:主源同步延迟/故障时自动兜底(阿里云与清华互为补充)
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
ARG CODEX_CLI_VERSION=0.144.3
ARG UV_VERSION=0.12.5

# === Stage 1: 前端构建 ===
FROM node:20-alpine AS frontend-builder
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /build
# 关键:corepack 不读 npm 的 registry 配置,且跨 RUN 不保留环境变量,
# 因此国内网络下最稳的做法是直接用 npm 安装 pnpm(npm 会读取 .npmrc 镜像源),
# 彻底绕开 corepack 再次联网下载 pnpm 的问题。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi && \
    npm install -g pnpm@9.10.0
# 让 pnpm 走镜像源安装依赖
RUN if [ "$USE_CN_MIRROR" = "1" ]; then pnpm config set registry "$NPM_REGISTRY"; fi
COPY frontend/package.json frontend/pnpm-lock.yaml ./
# pnpm store 缓存挂载: 锁文件不变时重复构建直接复用 (cache 层可被 GC, 不依赖镜像层缓存)
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# === Stage 1b: stock-sdk 插件依赖(可选,默认跳过) ===
# ⚠️ 合规提示: stock-sdk 通过 node bridge.mjs 抓取第三方财经网站(如东方财富)的行情接口,
#    未经对方授权,可能违反其服务条款并涉及交易所行情版权。默认不打包(INCLUDE_STOCKSDK=0)。
#    如确需启用,构建时传 --build-arg INCLUDE_STOCKSDK=1,即视为使用者知悉并自行承担合规责任。
# INCLUDE_STOCKSDK=0 时,本 stage 仅产出空 node_modules 目录,保证后续 COPY 不报错。
FROM node:20-bookworm-slim AS stocksdk-builder
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG INCLUDE_STOCKSDK=0
WORKDIR /build
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi
COPY backend/app/plugins/stocksdk/package.json backend/app/plugins/stocksdk/package-lock.json ./
# INCLUDE_STOCKSDK=1 时安装依赖;=0 时仅建空目录,使最终镜像不含 stock-sdk 依赖
RUN if [ "$INCLUDE_STOCKSDK" = "1" ]; then \
      npm ci; \
    else \
      mkdir -p /build/node_modules; \
    fi

# === Stage 1c: Codex CLI ===
# 固定版本保证镜像可复现；只复制安装产物到运行镜像，不保留 npm。
FROM node:20-bookworm-slim AS codex-builder
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
# 版本由顶层 ARG CODEX_CLI_VERSION 提供, 这里仅声明以继承, 不再重复默认值。
ARG CODEX_CLI_VERSION
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install --global --prefix /opt/codex "@openai/codex@${CODEX_CLI_VERSION}" \
    && CODEX_NATIVE="$(find /opt/codex -type f -path '*/vendor/*/bin/codex' -print -quit)" \
    && test -n "$CODEX_NATIVE" \
    && cp "$CODEX_NATIVE" /opt/codex-native \
    && chmod +x /opt/codex-native \
    && /opt/codex-native --version

# === Stage 2: Python 运行时 ===
FROM python:3.12-slim AS runtime
ARG USE_CN_MIRROR=1
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
ARG INCLUDE_STOCKSDK=0
ARG INCLUDE_OCR=1
ARG UV_VERSION
WORKDIR /app

# Node.js 运行时: 仅在启用 stock-sdk 插件时安装(供 node bridge.mjs 使用)。
# Codex CLI 从官方 npm 包提取原生二进制，不依赖运行时 Node.js。
# bookworm 自带 nodejs 18.19, 满足插件 engines>=18; --no-install-recommends 精简,
# 自带 libnode/libc-ares 等全部动态依赖, 无需手动补库。
# 国内构建走 apt mirror 已在 debian 镜像sources.list 配好, 无需额外换源。
# tesseract-ocr: 自选截图导入 INCLUDE_OCR=1(默认) 时安装; nodejs: 仅 INCLUDE_STOCKSDK=1 时安装
RUN apt-get update \
    && if [ "$INCLUDE_OCR" = "1" ]; then \
         apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
         && tesseract --version; \
       fi \
    && if [ "$INCLUDE_STOCKSDK" = "1" ]; then \
         apt-get install -y --no-install-recommends nodejs \
         && node --version; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# 安装固定版本 uv；国内镜像下按主源 → 备用源 → 官方源重试。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      pip install --no-cache-dir "uv==$UV_VERSION" -i "$PYPI_INDEX" || \
      pip install --no-cache-dir "uv==$UV_VERSION" -i "$PYPI_FALLBACK" || \
      pip install --no-cache-dir "uv==$UV_VERSION"; \
    else \
      pip install --no-cache-dir "uv==$UV_VERSION"; \
    fi

# Backend deps：这里只安装锁定依赖，不在源码 COPY 前构建一个空项目。
COPY backend/pyproject.toml backend/uv.lock ./
# uv 原生支持同时挂多个 index(主源 + 备用源),会自动在两源中查找,
# 比逐个重试更稳健 —— 任一源缺包时另一源补位。
# uv 全局缓存挂载: 锁文件不变时重复构建直接复用已下载的 wheel。
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$USE_CN_MIRROR" = "1" ]; then \
      export UV_DEFAULT_INDEX="$PYPI_INDEX" UV_EXTRA_INDEX_URL="$PYPI_FALLBACK"; \
    fi; \
    set -- --no-dev; \
    for extra in $BACKEND_EXTRAS; do \
      set -- "$@" --extra "$extra"; \
    done; \
    uv sync --frozen --no-install-project "$@"

# Backend code
# 注意:Docker 里 WORKDIR=/app, 而 config.py 的 _PROJECT_ROOT 是按开发布局
# (<root>/backend/app/) 推导的, 容器内会错算到 /。这里用环境变量显式指定
# 三个关键路径, 确保 static / tiers / data 都指向容器内正确位置。
COPY backend/app ./app
# stock-sdk 插件依赖: 从 stocksdk-builder 拷入。
# INCLUDE_STOCKSDK=0(默认) 时, stocksdk-builder 产出空目录,此处拷入空目录,
# 即最终镜像不含 stock-sdk 依赖,插件默认不可用。
# COPY --from 不受 .dockerignore 的 **/node_modules 规则影响。
COPY --from=stocksdk-builder /build/node_modules ./app/plugins/stocksdk/node_modules
COPY tiers.yaml /app/tiers.yaml
ENV STATIC_DIR=/app/static \
    TIERS_YAML=/app/tiers.yaml \
    DATA_DIR=/app/data

# Frontend 静态产物
COPY --from=frontend-builder /build/dist ./static

# Codex CLI 使用官方 npm 包携带的当前平台原生二进制，无需运行时 Node.js。
COPY --from=codex-builder /opt/codex-native /usr/local/bin/codex
RUN codex --version

# 非 root 运行 (UID 1000 与常见主机首个用户一致)。
# 注意: Linux 主机上 ./data 挂载卷的属主必须是 1000 (见 docs/deployment.md 迁移说明);
# macOS (osxfs) 不校验属主, 无影响。
RUN useradd --create-home --uid 1000 --gid 1000 tickflow \
    && chown -R tickflow:tickflow /app
USER tickflow

ENV PYTHONPATH=/app
# 兜底时区: 交易时段判断已在代码里显式用北京时间 (app/market_time.py),
# 此处让日志时间戳等其余 naive 时间也对齐北京时间。
ENV TZ=Asia/Shanghai
EXPOSE 3018
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3018/health', timeout=3)"]
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3018"]
