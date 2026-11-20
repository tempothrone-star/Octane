FROM node:22-bookworm-slim
RUN npm install --prefix /opt/octane-properties fast-check@4.3.0
ENV NODE_PATH=/opt/octane-properties/node_modules
