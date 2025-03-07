FROM alpine:3

RUN apk add --no-cache rsync

WORKDIR /rsync-util

COPY ./sync-storage.sh sync-storage.sh

RUN chmod +x ./sync-storage.sh

ENTRYPOINT [ "/bin/sh", "/rsync-util/sync-storage.sh" ]