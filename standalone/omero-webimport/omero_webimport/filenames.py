import posixpath
import re

"""
Upload filename sanitising for the web importer.

Reduces the filename a browser sends with each upload to a safe basename,
which becomes the imported file's name in OMERO. Both ``/`` and ``\\`` end
a directory component whatever the server's OS, since a browser on Windows
may send a full ``C:\\...`` path; ``os.path.basename`` would keep that path
on the Linux container. Pure (no OMERO or Django imports), so it unit-tests
offline like ``batch`` and ``jobs``.
"""


def safe_filename(name):
    """
    Reduce an uploaded filename to a safe basename.

    Args:
        name (str or None): The filename, or path, as the browser sent it.

    Returns:
        str: Its last path component with each run of characters outside
            ``A-Za-z0-9. _-`` replaced by ``_``, cut to 200 characters;
            ``"unnamed"`` when nothing is left.
    """
    name = posixpath.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9. _-]+", "_", name)
    return name[:200] or "unnamed"
