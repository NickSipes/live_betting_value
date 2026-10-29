FROM python:3.12-slim
WORKDIR /app
# pollers use only the standard library, except Web Push (live/push.py); model weights and priors ship in the image
RUN pip install --no-cache-dir pywebpush==2.5.0
COPY live/*.py live/model_weights.json live/calib.json live/priors.json live/def_priors.json live/def_form.json live/start.sh live/
COPY docs/ docs/
RUN chmod +x live/start.sh
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8766 DOCS_DIR=/data/docs DATA_DIR=/data/live
EXPOSE 8766
CMD ["/app/live/start.sh"]
