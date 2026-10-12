FROM node:24.21.0-alpine3.24@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1
WORKDIR /srv/fastify
COPY backends/fastify/package.json backends/fastify/package-lock.json ./
RUN npm ci --omit=dev
COPY backends/fastify/*.js ./
USER node
EXPOSE 8000
CMD ["node", "server.js"]
