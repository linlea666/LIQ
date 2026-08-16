FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ARG NEXT_PUBLIC_WS_URL=http://localhost:8000
ARG NEXT_PUBLIC_RADAR_API_BASE=http://localhost:8802

ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
# NEXT_PUBLIC_* 在 build 时被内联进 bundle，运行时改环境变量无效——
# 雷达地址变了必须重新 build 前端，不能只重启
ENV NEXT_PUBLIC_RADAR_API_BASE=$NEXT_PUBLIC_RADAR_API_BASE

# 限制 Node 堆内存上限，避免低内存机器（如 3.5GB 服务器）build 期触发 swap thrashing。
# 内存不够时立即抛 OOM 退出，而非死循环换页拖死整机。
ENV NODE_OPTIONS="--max-old-space-size=2048"

RUN npm run build

FROM node:20-alpine AS runner

WORKDIR /app
ENV NODE_ENV=production

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

EXPOSE 3000

CMD ["node", "server.js"]
