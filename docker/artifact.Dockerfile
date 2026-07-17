# syntax=docker/dockerfile:1.6
ARG ODOO_VERSION=19.0
ARG ODOO_BASE_RUNTIME_IMAGE
ARG ODOO_BASE_DEVTOOLS_IMAGE
ARG ODOO_ADDON_REPOSITORIES
ARG OPENUPGRADE_ADDON_REPOSITORY
ARG ODOO_PYTHON_SYNC_SKIP_ADDONS

FROM scratch AS project-payload
COPY /platform/config /payload/volumes/config
COPY /docker/scripts /payload/volumes/scripts
COPY /runtime /payload/opt/runtime
COPY /project /payload/opt/project
COPY /addons /payload/opt/project/addons

FROM ${ODOO_BASE_RUNTIME_IMAGE} AS addon-sources
ARG ODOO_ADDON_REPOSITORIES
ARG OPENUPGRADE_ADDON_REPOSITORY
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN --mount=type=secret,id=github_token \
    rm -rf /opt/extra_addons \
    && mkdir -p /opt/extra_addons \
    && GITHUB_TOKEN="$(cat /run/secrets/github_token 2>/dev/null || true)" \
       ODOO_ADDON_REPOSITORIES="${ODOO_ADDON_REPOSITORIES}" \
       /usr/local/bin/odoo-fetch-addons.sh

FROM ${ODOO_BASE_RUNTIME_IMAGE} AS base-runtime
ARG ODOO_ADDON_REPOSITORIES
ARG OPENUPGRADE_ADDON_REPOSITORY
ARG ODOO_PYTHON_SYNC_SKIP_ADDONS
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN rm -rf /opt/runtime /opt/project /opt/extra_addons /opt/launchplane/evidence /volumes/config /volumes/scripts \
    && mkdir -p /opt/runtime /opt/project /opt/extra_addons /opt/launchplane/evidence /volumes

COPY --from=project-payload /payload/ /
COPY --from=addon-sources --chown=ubuntu:ubuntu /opt/extra_addons /opt/extra_addons

RUN mkdir -p /volumes /opt/project \
    && rm -f /volumes/pyproject.toml /volumes/uv.lock \
    && ln -s /opt/project/pyproject.toml /volumes/pyproject.toml \
    && ln -s /opt/project/uv.lock /volumes/uv.lock

FROM ${ODOO_BASE_DEVTOOLS_IMAGE} AS base-devtools
ARG ODOO_ADDON_REPOSITORIES
ARG OPENUPGRADE_ADDON_REPOSITORY
ARG ODOO_PYTHON_SYNC_SKIP_ADDONS
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN rm -rf /opt/runtime /opt/project /opt/extra_addons /opt/launchplane/evidence /volumes/config /volumes/scripts \
    && mkdir -p /opt/runtime /opt/project /opt/extra_addons /opt/launchplane/evidence /volumes

COPY --from=project-payload /payload/ /
COPY --from=addon-sources --chown=ubuntu:ubuntu /opt/extra_addons /opt/extra_addons

RUN mkdir -p /volumes /opt/project \
    && rm -f /volumes/pyproject.toml /volumes/uv.lock \
    && ln -s /opt/project/pyproject.toml /volumes/pyproject.toml \
    && ln -s /opt/project/uv.lock /volumes/uv.lock \
    && rm -rf /opt/project/tools \
    && ln -s /volumes/tools /opt/project/tools

FROM base-runtime AS production
ARG TARGETPLATFORM
WORKDIR /opt/project
RUN --mount=type=cache,target=/home/ubuntu/.cache/uv,uid=1000,gid=1000,sharing=locked \
	TARGETPLATFORM="${TARGETPLATFORM}" ODOO_PYTHON_SYNC_SKIP_ADDONS="${ODOO_PYTHON_SYNC_SKIP_ADDONS}" /usr/local/bin/odoo-python-sync.sh prod
WORKDIR /
USER ubuntu

FROM base-devtools AS development
ARG TARGETPLATFORM
WORKDIR /opt/project

RUN --mount=type=cache,target=/home/ubuntu/.cache/uv,uid=1000,gid=1000,sharing=locked \
	TARGETPLATFORM="${TARGETPLATFORM}" ODOO_PYTHON_SYNC_SKIP_ADDONS="${ODOO_PYTHON_SYNC_SKIP_ADDONS}" /usr/local/bin/odoo-python-sync.sh dev
USER ubuntu
