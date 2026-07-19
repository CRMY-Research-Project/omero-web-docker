# OMERO webimport

This is a prototype based on William Moore's [initial implementation](https://gitlab.com/openmicroscopy/incubator/omero-webimport) to investigate the support of image import into OMERO from a web client.

## Install

Install for development:

    $ cd omero-webimport
    $ pip install -e .

    # Add to config:

    $ omero config append omero.web.apps omero_webimport

Then restart your OMERO.web server.

## Usage:

 - Go to {your-server}/omero_webimport
 - Drag files onto the page
 - Click Import to start upload and import

NB: The selected files will be uploaded to OMERO into a single **FileSet** and
then imported into OMERO. A FileSet in OMERO is a grouping of 1 or more files
that are part of the same OMERO Image or Images.
See https://docs.openmicroscopy.org/latest/omero/developers/ImportFS.html

Bio-Formats will parse the files in the upload, find the first FileSet and
import that. If any required files are missing, the import will fail. Also,
if there are additional files in the upload that are not part
of the FileSet identified by Bio-Formats, they will not be imported, although
they will be uploaded and linked to the FileSet in the OMERO DB.

This is a limitation of web-based import. Normally, Bio-Formats is used
on the client-side to parse files so that all the required files can be
automatically uploaded for import.

## Large files, chunked upload and async import

Uploads are **chunked** (default 64 MiB), and each chunk is retried on
failure, so a dropped connection retries one chunk rather than the whole
multi-GB file (staged under `WEBIMPORT_STAGING_DIR`).

The import itself runs **asynchronously**: `upload/complete/` returns a
`job_id` immediately (HTTP 202) and the browser polls
`upload/status/<job_id>/` until the import finishes. The import runs in a
background thread that **rejoins the user's OMERO session** by UUID (so the
images are owned by the uploading user, not a service account).

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `WEBIMPORT_STAGING_DIR` | `/opt/omero/web/webimport_staging` | chunk + job state (volume-back it) |
| `WEBIMPORT_MAX_FILE_MB` | `20480` | per-file size cap |
| `WEBIMPORT_MAX_CHUNK_MB` | `100` | per-chunk size cap |
| `WEBIMPORT_ASYNC` | `1` | set `0` to import synchronously (blocks the request) |

### Caveats (async mode)

- The background thread needs the OMERO session to stay valid for the whole
  import. Very long imports could exceed the session's `timeToLive`; the
  import activity normally keeps it alive, but this is the main risk.
- The thread lives inside a gunicorn worker; if the worker is recycled
  (`max_requests`) mid-import the job is lost (its staged files are kept for
  retry). For heavy production use, replace the thread with a real task
  queue (Celery / Django-Q) — the job registry API (`jobs.py`) was kept
  minimal to allow that swap. Set `WEBIMPORT_ASYNC=0` if threads are
  undesirable in your deployment.
