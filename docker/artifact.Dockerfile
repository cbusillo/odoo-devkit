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
    && repositories="${ODOO_ADDON_REPOSITORIES}" \
    && if [[ -n "${OPENUPGRADE_ADDON_REPOSITORY}" ]]; then repositories="${repositories:+${repositories},}${OPENUPGRADE_ADDON_REPOSITORY}"; fi \
    && GITHUB_TOKEN="$(cat /run/secrets/github_token 2>/dev/null || true)" \
       ODOO_ADDON_REPOSITORIES="${repositories}" \
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

RUN /venv/bin/python /volumes/scripts/odoo_release_inventory.py --base /opt/launchplane/evidence/base-runtime-inventory.json

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

RUN /venv/bin/python /volumes/scripts/odoo_release_inventory.py --base-tools /opt/launchplane/evidence/base-devtools-inventory.json

FROM base-runtime AS production
ARG TARGETPLATFORM
COPY /release-inventory-inputs.json /opt/launchplane/evidence/release-inventory-inputs.json
COPY --from=base-devtools /opt/launchplane/evidence/base-devtools-inventory.json /opt/launchplane/evidence/base-devtools-inventory.json
WORKDIR /opt/project
RUN --mount=type=cache,target=/home/ubuntu/.cache/uv,uid=1000,gid=1000,sharing=locked \
	TARGETPLATFORM="${TARGETPLATFORM}" ODOO_PYTHON_SYNC_SKIP_ADDONS="${ODOO_PYTHON_SYNC_SKIP_ADDONS}" /usr/local/bin/odoo-python-sync.sh prod
RUN /venv/bin/python /volumes/scripts/odoo_release_inventory.py \
    /opt/launchplane/evidence/release-inventory-inputs.json /opt/launchplane/evidence/release-compatibility.json \
    && rm /opt/launchplane/evidence/release-inventory-inputs.json
WORKDIR /
USER ubuntu

FROM base-devtools AS development
ARG TARGETPLATFORM
WORKDIR /opt/project

RUN --mount=type=cache,target=/home/ubuntu/.cache/uv,uid=1000,gid=1000,sharing=locked \
	TARGETPLATFORM="${TARGETPLATFORM}" ODOO_PYTHON_SYNC_SKIP_ADDONS="${ODOO_PYTHON_SYNC_SKIP_ADDONS}" /usr/local/bin/odoo-python-sync.sh dev
USER ubuntu
