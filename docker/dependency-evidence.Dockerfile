# syntax=docker/dockerfile:1.6
ARG ARTIFACT_IMAGE

FROM --platform=$TARGETPLATFORM ${ARTIFACT_IMAGE} AS artifact

FROM scratch
COPY --from=artifact /opt/launchplane/evidence/dependency-provenance.json /dependency-provenance.json
