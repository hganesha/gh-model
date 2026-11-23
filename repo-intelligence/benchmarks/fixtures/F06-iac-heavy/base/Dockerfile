FROM python:3.12-slim
COPY . /srv
RUN pip install /srv
CMD ["flask", "--app", "app.server", "run", "--host", "0.0.0.0"]
