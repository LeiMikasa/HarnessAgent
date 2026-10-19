FROM node:24-bookworm-slim

# 构建镜像时安装开发工具
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-venv \
       python3-pip \
       git \
       bash \
       ripgrep \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 容器内默认工作目录
WORKDIR /workspace

# 运行任务时使用普通用户
USER node

CMD ["bash"]