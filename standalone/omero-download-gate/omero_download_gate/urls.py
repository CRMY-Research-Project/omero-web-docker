from django.urls import re_path

from . import views

urlpatterns = [
    # user page: submit + track requests
    re_path(r'^$', views.index, name='omero_download_gate_index'),
    re_path(r'^request/$', views.create_request,
            name='omero_download_gate_request'),
    re_path(r'^requests/mine/$', views.my_requests,
            name='omero_download_gate_mine'),

    # requester access: the request form's dataset picker, and the
    # "My access" browser (grants -> datasets -> images). Both reach data
    # outside the requester's groups via the service account - the picker
    # for the catalogue + policy datasets, the browser for granted data only
    re_path(r'^datasets/requestable/$', views.requestable_datasets,
            name='omero_download_gate_requestable'),
    re_path(r'^access/mine/$', views.my_access,
            name='omero_download_gate_my_access'),
    re_path(r'^access/(?P<scope>project|dataset)/(?P<scope_id>[0-9]+)/$',
            views.access_browse, name='omero_download_gate_access_browse'),

    # admin review
    re_path(r'^review/$', views.review,
            name='omero_download_gate_review'),
    re_path(r'^review/list/$', views.review_list,
            name='omero_download_gate_review_list'),
    re_path(r'^review/action/$', views.review_action,
            name='omero_download_gate_review_action'),

    # dataset access policies (I2): GET current / POST to set
    # (dataset owner, a steward approver, or a global admin)
    re_path(r'^policy/dataset/(?P<dataset_id>[0-9]+)/$', views.dataset_policy,
            name='omero_download_gate_policy'),

    # access grants (I3): my active grants, the review/revoke list, revoke
    re_path(r'^grants/mine/$', views.my_grants,
            name='omero_download_gate_my_grants'),
    re_path(r'^grants/$', views.grants_list,
            name='omero_download_gate_grants'),
    re_path(r'^grants/revoke/$', views.grant_revoke,
            name='omero_download_gate_grant_revoke'),

    # governance audit log (I5): admin sees all, steward sees their datasets
    re_path(r'^audit/$', views.audit_log,
            name='omero_download_gate_audit'),

    # supporting documents (admin or owner)
    re_path(r'^doc/(?P<request_id>[0-9a-f]{32})/(?P<filename>[^/]+)$',
            views.download_doc, name='omero_download_gate_doc'),

    # gated downloads
    re_path(r'^files/image/(?P<image_id>[0-9]+)/$',
            views.list_image_files, name='omero_download_gate_files'),
    re_path(r'^status/image/(?P<image_id>[0-9]+)/$',
            views.download_status, name='omero_download_gate_status'),
    re_path(r'^download/image/(?P<image_id>[0-9]+)/$',
            views.download_image, name='omero_download_gate_download'),
]
