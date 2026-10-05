import logging

from django.http import HttpResponse, JsonResponse

from . import assoc

"""
Middleware that keeps slide label and macro images from anonymous visitors.

Label and macro images share a fileset, and therefore a group, with the slide
they come from, so OMERO's own permissions cannot hide them from the public
user. This middleware refuses them to anonymous and public-user sessions on
every route that resolves an image id, and drops them from thumbnail batches;
logged-in users are unaffected. Image names come from the gate's service
account. If that lookup fails, the request is refused rather than risking a
label leak.

Registered in 01-default-webapps.omero after Django's session middleware.
"""

logger = logging.getLogger(__name__)


def _lookup_names(image_ids):
    """
    Fetch image names across all groups through the gate's service account.

    Args:
        image_ids (list): Image ids to name.

    Returns:
        dict: {image_id: name} for the ids that exist.

    Raises:
        RuntimeError: If no service account is configured or it cannot log in.
    """
    # Lazy import: views pulls in omero.gateway, which loading this module at
    # Django startup should not depend on.
    from .views import _service_connection
    gateway = _service_connection()
    if gateway is None:
        raise RuntimeError(
            "DOWNLOAD_GATE_SERVICE_USER/PASS are not set; the associated-image "
            "guard cannot name images")
    try:
        return {image.getId(): image.getName()
                for image in gateway.getObjects("Image", image_ids)}
    finally:
        gateway.close()


_CACHE = assoc.AssociatedImageCache(_lookup_names)


class AssociatedImageGuard:
    """
    Associated image guard middleware.

    Inspects each request's resolved route before its view runs and refuses or
    filters label and macro images for anonymous sessions.

    Attributes:
        get_response (callable): The next handler in Django's middleware chain.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        """
        Pass the request down the middleware chain unchanged.

        Args:
            request (HttpRequest): The incoming request.

        Returns:
            HttpResponse: The downstream response.
        """
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """
        Refuse or filter associated images for anonymous sessions.

        Args:
            request (HttpRequest): The incoming request.
            view_func (callable): The resolved view (unused).
            view_args (tuple): Positional view arguments (unused).
            view_kwargs (dict): Keyword arguments resolved from the path.

        Returns:
            HttpResponse: A 403 or 503 refusal, or an empty thumbnail batch;
                None lets the view run (possibly with associated ids removed
                from a thumbnail batch).
        """
        if not assoc.session_is_public(request.session):
            return None
        match = request.resolver_match
        path_ids, batch_ids = assoc.requested_image_ids(
            match.url_name if match is not None else None, view_kwargs,
            request.GET.getlist("id"))
        if not path_ids and not batch_ids:
            return None
        try:
            blocked = _CACHE.associated(path_ids + batch_ids)
        except Exception:
            logger.exception("Associated-image lookup failed; refusing %s",
                             request.path)
            return HttpResponse("Image access check unavailable.",
                                status=503, content_type="text/plain")
        if any(iid in blocked for iid in path_ids):
            return HttpResponse(
                "This image is not available to anonymous visitors.",
                status=403, content_type="text/plain")
        if blocked and batch_ids:
            kept = [str(iid) for iid in batch_ids if iid not in blocked]
            if not kept:
                return JsonResponse({})
            # QueryDicts are immutable; swap in a filtered copy for the view
            query = request.GET.copy()
            query.setlist("id", kept)
            request.GET = query
        return None
