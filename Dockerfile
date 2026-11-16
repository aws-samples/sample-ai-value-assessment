# aiva as a container image, for ECS Fargate, EKS, CodeBuild or any Docker host.
# Build from a checkout of the version you want: docker build -t aiva .
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /src
COPY pyproject.toml README.md ./
COPY aiva ./aiva
RUN pip install --no-cache-dir . && rm -rf /src && useradd --create-home aiva
USER aiva
WORKDIR /home/aiva
ENTRYPOINT ["aiva"]
