FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt /app
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install -r requirements.txt

COPY ./main.py /app

RUN apk add --no-cache rsync

RUN echo "[dt_data]" > /etc/rsyncd.conf && \
    echo "path = /var/tmp/dt_data" >> /etc/rsyncd.conf

CMD ["/bin/sh", "-c", "rsync --daemon && python3 main.py"]