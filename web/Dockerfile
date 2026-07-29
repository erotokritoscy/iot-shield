# The React dashboard: build with node, serve with nginx (X3).
#
# Deliberately its own image rather than an addition to the root Python one — the two have
# nothing in common at runtime, and a Python image carrying a node toolchain to serve static
# files would be the worst of both.

FROM node:22-alpine AS build
WORKDIR /app
# Dependencies first, so a source-only change does not re-resolve the tree.
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# The image's entrypoint runs envsubst over everything in /etc/nginx/templates, so the proxy
# targets stay 12-factor: one image, per-environment URLs from the environment (D1).
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
ENV IOTSHIELD_API_URL=http://api:8000 \
    IOTSHIELD_PIPELINE_API_URL=http://pipeline-api:8100
EXPOSE 80
