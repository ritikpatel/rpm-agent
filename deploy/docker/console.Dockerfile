# Clinician console: static React build served by nginx, which also proxies /api and /ws to the API service.
FROM node:22-alpine AS build
WORKDIR /app
COPY apps/clinician-console/package.json apps/clinician-console/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/clinician-console/ ./
RUN npm run build

FROM nginx:1.29-alpine
ENV API_UPSTREAM=api:8000 AUTH_ORIGIN=""
COPY deploy/docker/console/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
