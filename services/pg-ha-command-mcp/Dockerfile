FROM fullstorydev/grpcurl:v1.9.3 AS grpcurl
FROM python:3.12-slim
WORKDIR /app
COPY --from=grpcurl /bin/grpcurl /usr/local/bin/grpcurl
COPY requirements.txt protocol.py server.py agent.py ./
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /var/lib/pgha-command-agent && \
    chown -R 65532:65532 /var/lib/pgha-command-agent
USER 65532:65532
ENTRYPOINT ["python", "agent.py"]
