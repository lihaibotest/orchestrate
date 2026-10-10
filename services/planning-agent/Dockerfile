FROM orchestrator-agent-standalone:1.1.0
USER 0
LABEL org.opencontainers.image.title="planning-agent-stub" \
      org.opencontainers.image.version="offline-v1"
COPY services/planning-agent/app/planning_agent_stub.py /opt/planning-agent/planning_agent_stub.py
EXPOSE 18000
ENTRYPOINT ["/usr/local/bin/python3.10", "/opt/planning-agent/planning_agent_stub.py"]
