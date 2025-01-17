FROM python:3.9.7-slim-buster as build
# python:3.7-slim-stretch (using Debian 9) has fewer issues with invalid https certificiates than python:3.7-slim-buster (using Debian 10).
# As of 2019-11-29, python:3.8-slim-stretch doesn't exist. See https://github.com/docker-library/python/issues/428
WORKDIR /app
COPY requirements.txt .
RUN set -x && \
    pip install --no-cache-dir -U pip wheel && \
    pip install --no-cache-dir -r ./requirements.txt
# Note: Regarding SECLEVEL, see https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=927461
# Lowering the SECLEVEL causes more https certificates to be valid.
COPY ircurltitlebot ircurltitlebot
RUN set -x && \
    groupadd -g 999 app && \
    useradd -r -m -u 999 -g app app
USER app
ENTRYPOINT ["python", "-m", "ircurltitlebot"]
CMD ["--config-path", "/config/config.yaml"]
STOPSIGNAL SIGINT

FROM build as test
WORKDIR /app
USER root
COPY Makefile pylintrc pyproject.toml requirements-dev.in setup.cfg vulture.txt ./
RUN set -x && \
    pip install --no-cache-dir -U -r requirements-dev.in && \
    apt-get update && apt-get -y install make && \
    make test

FROM build
