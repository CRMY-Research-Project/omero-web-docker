#
# Copyright (c) 2019 University of Dundee.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from django.urls import re_path

from . import views

urlpatterns = [

    # index 'home page' of the app
    re_path(r'^$', views.index, name='omero_webimport_index'),

    # POST files to import
    re_path(r'^import/$', views.submit_import, name="omero_webimport_import"),

    # GET datasets the user can import into (for the target dropdown)
    re_path(r'^datasets/$', views.datasets, name="omero_webimport_datasets"),

    # chunked upload flow (large files, retryable chunks - TC-19)
    re_path(r'^upload/begin/$', views.begin_upload,
            name="omero_webimport_begin"),
    re_path(r'^upload/chunk/$', views.upload_chunk,
            name="omero_webimport_chunk"),
    re_path(r'^upload/complete/$', views.complete_upload,
            name="omero_webimport_complete"),
    re_path(r'^upload/status/(?P<job_id>[0-9a-f]{32})/$',
            views.import_status, name="omero_webimport_status"),
]
