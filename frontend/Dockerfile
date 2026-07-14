FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder
WORKDIR /app
COPY package*.json .
RUN npm ci
COPY . .
RUN npm run build

# Audit-F35 — digest-pinned like every other base image in the repo, so
# a rebuild of a given commit is byte-reproducible (a floating `alpine`
# tag pulled a different nginx/OpenSSL/musl build over time). Refresh
# deliberately: `docker buildx imagetools inspect nginx:alpine`.
FROM nginx:alpine@sha256:54f2a904c251d5a34adf545a72d32515a15e08418dae0266e23be2e18c66fefa
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
