FROM ollama/ollama:latest

USER root
# run.sh uses curl for readiness; bash for /usr/bin/env bash shebang
RUN if command -v apt-get >/dev/null 2>&1; then \
      apt-get update && apt-get install -y --no-install-recommends curl bash \
      && rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
      apk add --no-cache curl bash; \
    fi

ENV OLLAMA_NUM_THREADS=2

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/bin/bash", "/run.sh"]
