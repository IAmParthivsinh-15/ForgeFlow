FROM node:24-alpine AS build
WORKDIR /app
COPY apps/frontend/package.json apps/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/frontend ./
RUN npm run build

FROM nginx:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
