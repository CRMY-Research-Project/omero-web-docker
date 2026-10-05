import threading
import time

"""
Associated slide image rules for the anonymous-visitor guard.

A whole-slide scan ships its scanner label and macro overview as extra
images inside the slide's own fileset, so OMERO files them in the slide's
group and the public user can open them like any other image. This module
holds the pure, Django-free logic the guard middleware relies on: which
image ids a request addresses, whether a name marks an associated image,
and a time-bounded cache of that verdict.

Includes:
    EXCLUDED_NAME_PARTS: Name fragments that mark an associated image.
    is_associated_name: Classify one image name.
    session_is_public: Tell an anonymous OMERO.web session from a login.
    requested_image_ids: Pull the image ids a resolved request addresses.
    AssociatedImageCache: Remember verdicts so tile floods cost one lookup.
"""

# Same rule as the landing page's listing filter, so what listings hide is
# exactly what anonymous visitors cannot open.
EXCLUDED_NAME_PARTS = ("label", "macro")

# URL kwargs that carry an image id across webgateway, webclient, the JSON
# API and iviewer (surveyed on omero-web 5.33).
PATH_ID_KWARGS = ("iid", "imageId", "image_id", "img_id")

# The JSON API calls every object "object_id"; only this route means an image.
API_IMAGE_URL_NAMES = ("api_image",)

# Thumbnail batch endpoints take image ids as repeated ?id= parameters.
THUMBNAIL_BATCH_URL_NAMES = ("webgateway_get_thumbnails_json",
                             "get_thumbnails_json")


def is_associated_name(name):
    """
    Decide whether an image name marks a scanner label or macro image.

    Args:
        name (str): The OMERO image name; None is treated as empty.

    Returns:
        bool: True when the name contains any of EXCLUDED_NAME_PARTS.
    """
    lower = (name or "").lower()
    return any(part in lower for part in EXCLUDED_NAME_PARTS)


def session_is_public(session):
    """
    Tell whether a Django session belongs to an anonymous visitor.

    OMERO.web keeps its connector as a dict under "connector". A visitor who
    has not logged in either has none yet or carries the public user's,
    which is flagged is_public.

    Args:
        session (Mapping): The request's session.

    Returns:
        bool: True for anonymous and public-user sessions.
    """
    connector = session.get("connector")
    if connector is None:
        return True
    if hasattr(connector, "get"):
        return bool(connector.get("is_public"))
    return bool(getattr(connector, "is_public", False))


def _to_ids(values):
    """
    Convert raw id strings to positive ints, dropping anything else.

    Args:
        values (Iterable): Values from URL kwargs or the query string.

    Returns:
        list: The values that are plain positive integers, in order.
    """
    ids = []
    for value in values:
        text = str(value).strip()
        if text.isascii() and text.isdigit() and int(text) > 0:
            ids.append(int(text))
    return ids


def requested_image_ids(url_name, view_kwargs, id_params):
    """
    Pull the image ids a resolved OMERO.web request addresses.

    Args:
        url_name (str): The resolved route name, or None.
        view_kwargs (Mapping): Keyword arguments Django resolved from the path.
        id_params (Iterable): The request's repeated ?id= values.

    Returns:
        tuple: (path_ids, batch_ids). path_ids come from the URL path and are
            refused outright; batch_ids come from a thumbnail batch and are
            filtered out instead.
    """
    path_values = [view_kwargs[key] for key in PATH_ID_KWARGS
                   if view_kwargs.get(key)]
    if url_name in API_IMAGE_URL_NAMES and view_kwargs.get("object_id"):
        path_values.append(view_kwargs["object_id"])
    batch_values = id_params if url_name in THUMBNAIL_BATCH_URL_NAMES else ()
    return _to_ids(path_values), _to_ids(batch_values)


class AssociatedImageCache:
    """
    Associated image verdict cache.

    Remembers, per image id, whether the image is a scanner label or macro
    image, so the flood of tile requests a viewer makes for one slide costs a
    single name lookup. Verdicts expire after a TTL so a renamed image is
    picked up again.

    Attributes:
        lookup (callable): Maps a list of image ids to {id: name} for the ids
            that exist; may raise when names cannot be fetched.
        ttl (float): Seconds a verdict stays valid.
        max_entries (int): Size bound; the cache is emptied when exceeded.
        clock (callable): Returns the current time in seconds.
        _verdicts (dict): image id -> (is_associated, time it was recorded).
        _lock (threading.Lock): Guards _verdicts across request threads.
    """

    def __init__(self, lookup, ttl=3600.0, max_entries=100000,
                 clock=time.monotonic):
        self.lookup = lookup
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self._verdicts = {}
        self._lock = threading.Lock()

    def associated(self, image_ids):
        """
        Return which of the given images are label or macro images.

        Steps:
            1. Serve fresh verdicts from the cache.
            2. Name the rest in one lookup, outside the lock.
            3. Record a verdict for every looked-up id; an id that does not
               exist counts as not associated (its view will 404 anyway).

        Args:
            image_ids (Iterable): Image ids to classify.

        Returns:
            set: The subset of image_ids that are associated images.

        Raises:
            Exception: Whatever lookup raises; nothing is cached in that case.
        """
        now = self.clock()
        found, missing = set(), []
        with self._lock:
            for iid in set(image_ids):
                entry = self._verdicts.get(iid)
                if entry is not None and now - entry[1] < self.ttl:
                    if entry[0]:
                        found.add(iid)
                else:
                    missing.append(iid)
        if not missing:
            return found
        names = self.lookup(sorted(missing))
        with self._lock:
            if len(self._verdicts) + len(missing) > self.max_entries:
                self._verdicts.clear()
            for iid in missing:
                verdict = is_associated_name(names.get(iid))
                self._verdicts[iid] = (verdict, now)
                if verdict:
                    found.add(iid)
        return found
