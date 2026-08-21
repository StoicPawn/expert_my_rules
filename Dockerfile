FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN mkdir -p /data/workspaces
EXPOSE 8000
CMD ["awb", "serve", "--host", "0.0.0.0", "--port", "8000", "--workspaces", "/data/workspaces"]
