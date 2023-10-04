FROM python:alpine AS builder
WORKDIR  /home/playlist
RUN set eux; \
  addgroup -S playlist; \
  adduser -S playlist -G playlist -h /home/playlist -s /bin/sh;
USER playlist
COPY --chown=playlist:playlist docker/requirements.txt .
RUN set eux; \
  python3 -m pip install -r requirements.txt;

FROM python:alpine
WORKDIR  /home/playlist
RUN set eux; \
  addgroup -S playlist; \
  adduser -S playlist -G playlist -h /home/playlist -s /bin/sh;
USER playlist
COPY --from=builder --chown=playlist:playlist /home/playlist .
COPY --chown=playlist:playlist docker/dl.py .
ENTRYPOINT [ "python3", "dl.py" ]
