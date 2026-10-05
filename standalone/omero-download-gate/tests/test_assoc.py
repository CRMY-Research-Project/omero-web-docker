import pytest

from omero_download_gate import assoc

"""
Unit tests for the associated-image guard's pure rules.

No OMERO or Django required, so they run in plain pytest and in CI.
"""


# ----- Name rule -----

@pytest.mark.parametrize("name, expected", [
    ("case01.svs [label image]", True),
    ("case01.svs [macro image]", True),
    ("CASE01 LABEL.png", True),
    ("case01.svs", False),
    ("", False),
    (None, False),
])
def test_is_associated_name(name, expected):
    """
    Match label and macro names case-insensitively, and nothing else.
    """
    assert assoc.is_associated_name(name) is expected


# ----- Session classification -----

def test_session_without_connector_is_public():
    """
    Treat a visitor who has not logged in yet as anonymous.
    """
    assert assoc.session_is_public({}) is True


def test_public_user_connector_is_public():
    """
    Treat the shared public-user session as anonymous.
    """
    assert assoc.session_is_public(
        {"connector": {"is_public": True, "user_id": 9}}) is True


def test_logged_in_connector_is_not_public():
    """
    Leave a real login alone.
    """
    assert assoc.session_is_public(
        {"connector": {"is_public": False, "user_id": 5}}) is False


def test_connector_object_form_is_supported():
    """
    Read is_public from a connector object as well as a dict.
    """
    class Connector:
        is_public = False
    assert assoc.session_is_public({"connector": Connector()}) is False


# ----- Image ids a request addresses -----

@pytest.mark.parametrize("kwargs", [
    {"iid": "12"}, {"imageId": "12"}, {"image_id": "12"}, {"img_id": "12"},
])
def test_path_kwargs_are_image_ids(kwargs):
    """
    Pick up every kwarg name OMERO.web uses for an image id.
    """
    assert assoc.requested_image_ids("any_route", kwargs, []) == ([12], [])


def test_object_id_counts_only_on_the_api_image_route():
    """
    Count the JSON API's object_id as an image id on api_image only.
    """
    kwargs = {"object_id": "7", "api_version": "0"}
    assert assoc.requested_image_ids("api_image", kwargs, []) == ([7], [])
    # api_dataset etc. also use object_id, which is not an image there
    assert assoc.requested_image_ids("api_dataset", kwargs, []) == ([], [])


def test_thumbnail_batch_ids_come_from_the_query():
    """
    Read ?id= values as image ids on both thumbnail batch routes.
    """
    for name in ("webgateway_get_thumbnails_json", "get_thumbnails_json"):
        assert assoc.requested_image_ids(
            name, {"w": "96"}, ["3", "4"]) == ([], [3, 4])


def test_id_query_is_ignored_elsewhere():
    """
    Ignore ?id= on other routes, e.g. the tree API where it is a dataset id.
    """
    assert assoc.requested_image_ids("api_images", {}, ["3"]) == ([], [])


def test_non_numeric_and_non_positive_values_are_dropped():
    """
    Keep only plain positive integers, including ASCII-only digits.
    """
    path, batch = assoc.requested_image_ids(
        "get_thumbnails_json", {"iid": "x"}, ["0", "-1", "²", " 5 ", "a"])
    assert (path, batch) == ([], [5])


# ----- Verdict cache -----

class FakeLookup:
    """
    Fake name lookup.

    Serves names from a dict and records every batch it is asked for.

    Attributes:
        names (dict): image id -> name for the ids that "exist".
        calls (list): Each batch of ids requested, in call order.
    """

    def __init__(self, names):
        self.names = names
        self.calls = []

    def __call__(self, ids):
        """
        Record the batch and return names for the ids that exist.

        Args:
            ids (list): Image ids to name.

        Returns:
            dict: {image_id: name} for known ids.
        """
        self.calls.append(list(ids))
        return {iid: self.names[iid] for iid in ids if iid in self.names}


def test_cache_classifies_and_reuses_verdicts():
    """
    Classify a batch once and answer repeats from the cache.
    """
    lookup = FakeLookup({1: "slide.svs", 2: "slide.svs [label image]"})
    cache = assoc.AssociatedImageCache(lookup, clock=lambda: 100.0)
    assert cache.associated([1, 2]) == {2}
    assert cache.associated([2, 1]) == {2}
    assert lookup.calls == [[1, 2]]


def test_cache_looks_up_only_unknown_ids():
    """
    Send only ids without a fresh verdict to the lookup.
    """
    lookup = FakeLookup({1: "a", 2: "b macro", 3: "c"})
    cache = assoc.AssociatedImageCache(lookup, clock=lambda: 0.0)
    cache.associated([1])
    assert cache.associated([1, 2, 3]) == {2}
    assert lookup.calls == [[1], [2, 3]]


def test_missing_ids_count_as_not_associated():
    """
    Treat an id the lookup cannot find as an ordinary image.
    """
    cache = assoc.AssociatedImageCache(FakeLookup({}), clock=lambda: 0.0)
    assert cache.associated([404]) == set()


def test_verdicts_expire_after_ttl():
    """
    Look an id up again once its verdict is older than the TTL.
    """
    now = [0.0]
    lookup = FakeLookup({1: "x"})
    cache = assoc.AssociatedImageCache(lookup, ttl=10, clock=lambda: now[0])
    cache.associated([1])
    now[0] = 9.0
    cache.associated([1])
    now[0] = 11.0
    cache.associated([1])
    assert lookup.calls == [[1], [1]]


def test_lookup_failure_propagates_and_caches_nothing():
    """
    Surface a failed lookup so the guard can refuse, and cache nothing.
    """
    def broken(ids):
        raise RuntimeError("no service account")
    cache = assoc.AssociatedImageCache(broken, clock=lambda: 0.0)
    with pytest.raises(RuntimeError):
        cache.associated([1])
    assert cache._verdicts == {}


def test_cache_is_emptied_when_it_would_exceed_its_bound():
    """
    Drop all verdicts rather than grow past max_entries.
    """
    lookup = FakeLookup({i: "x" for i in range(10)})
    cache = assoc.AssociatedImageCache(lookup, max_entries=3,
                                       clock=lambda: 0.0)
    cache.associated([1, 2, 3])
    cache.associated([4])
    assert set(cache._verdicts) == {4}
