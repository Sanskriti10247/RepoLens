# RepoLens test sandbox. Built by the operator from trusted inputs only; repository code is
# mounted read-only at run time and never installed into the image.
FROM python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && useradd --uid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin sandbox

COPY runner.py /opt/repolens/runner.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["python", "/opt/repolens/runner.py"]
