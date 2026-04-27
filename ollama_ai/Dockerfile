FROM ollama/ollama

USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl bash \
  && rm -rf /var/lib/apt/lists/*

ENV OLLAMA_NUM_THREADS=2

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/bin/bash", "/run.sh"]
