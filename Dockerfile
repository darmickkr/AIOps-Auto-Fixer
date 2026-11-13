FROM ubuntu:latest
RUN apt-get update && apt-get isntall -y curl
CMD ["echo", "AIOps Agent Test"]