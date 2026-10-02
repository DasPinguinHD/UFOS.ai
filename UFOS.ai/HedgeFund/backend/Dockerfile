FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
# Inside a container the backend has to listen on all interfaces, so the
# loopback default is explicitly overridden (api_auth.py). Publish the ports on
# the host's loopback only (docker run -p 127.0.0.1:<port>:<port> …) and mount
# ./data so the app can read the per-start token file data/api-token.
ENV WS_HOST=0.0.0.0 HTTP_HOST=0.0.0.0 ALLOW_REMOTE_ACCESS=true
EXPOSE 8865 8867
CMD ["python", "main.py"]
