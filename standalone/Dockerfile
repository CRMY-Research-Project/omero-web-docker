ARG PARENT_IMAGE=openmicroscopy/omero-web:latest
FROM ${PARENT_IMAGE}
MAINTAINER ome-devel@lists.openmicroscopy.org.uk

USER root

RUN mkdir /sites

RUN /opt/omero/web/venv3/bin/pip install \
        omero-figure \
        omero-iviewer \
        omero-fpbioimage \
        omero-mapr \
        omero-parade \
        omero-autotag \
        omero-tagsearch \
        whitenoise \
        django-redis  # WS-M: Redis cache/session backend (docker-compose.prod.yml)

ADD omero-webimport /omero-webimport

RUN /opt/omero/web/venv3/bin/pip install -e /omero-webimport/

ADD omero-download-gate /omero-download-gate

RUN /opt/omero/web/venv3/bin/pip install -e /omero-download-gate/

# Writable state dir for the download gate (back it with a named volume
# in docker-compose so requests survive container rebuilds)
RUN mkdir -p /opt/omero/web/download_gate \
    && chown omero-web /opt/omero/web/download_gate

# Staging dir for chunked web uploads (also volume-backed)
RUN mkdir -p /opt/omero/web/webimport_staging \
    && chown omero-web /opt/omero/web/webimport_staging

# WS-F: optionally install the CRM landing page as a pip package (the
# production alternative to the docker-compose dev volume-mount). Pass a
# pip-installable git ref at build time, e.g.:
#   docker build --build-arg LANDING_PAGE_GIT=\
#     'git+https://github.com/CRMY-Research-Project/omero_crm_landing_page.git@v0.1#egg=omero_crm_landing_page'
# The referenced repo/tag must contain the COMPILED Svelte assets (run
# `npm run build` and commit omero_crm_landing_page/templates+static, or
# build them in CI before tagging). When set, also drop the two landing-page
# volume mounts from docker-compose.yml (see docs/landing-page-pip-migration.md).
# Left empty by default so the existing volume-mount dev flow is unchanged.
ARG LANDING_PAGE_GIT=""
RUN if [ -n "$LANDING_PAGE_GIT" ]; then \
        echo "Installing landing page from $LANDING_PAGE_GIT" && \
        /opt/omero/web/venv3/bin/pip install "$LANDING_PAGE_GIT" ; \
    else \
        echo "LANDING_PAGE_GIT unset - using docker-compose volume mount" ; \
    fi

# dependencies for local site
# RUN /opt/omero/web/venv3/bin/pip install \
        # inertia-django \
        # django-vite

ADD 01-default-webapps.omero /opt/omero/web/config/

USER omero-web
