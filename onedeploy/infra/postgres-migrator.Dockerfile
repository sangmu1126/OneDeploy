FROM node:22-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY postgres-migrator.js ./
COPY migrations ./migrations
CMD ["node", "postgres-migrator.js"]
