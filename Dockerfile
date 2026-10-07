FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY outreach_desk ./outreach_desk
RUN pip install --no-cache-dir .
ENV OUTREACH_DESK_HOME=/data
VOLUME /data
EXPOSE 8000
CMD ["outreach-desk", "--host", "0.0.0.0", "--no-browser"]
