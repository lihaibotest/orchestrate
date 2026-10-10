FROM orchestrator-agent-standalone:1.1.0
USER 0
LABEL org.opencontainers.image.title="orchestrator-gateway.v1" \
      org.opencontainers.image.version="offline-v1"
COPY services/orchestrator-gateway/app/orchestrator_gateway_dev.py /opt/orchestrator-gateway/orchestrator_gateway_dev.py
COPY services/orchestrator-gateway/entrypoint.sh /opt/orchestrator-gateway/entrypoint.sh
COPY scripts/orchestrator_gateway_dev.py /opt/orchestrator-gateway/scripts/orchestrator_gateway_dev.py
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/python3.10", "/opt/orchestrator-gateway/orchestrator_gateway_dev.py"]
