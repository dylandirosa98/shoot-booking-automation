from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("shoots/", views.shoots_list, name="shoots_list"),
    path("edits/", views.edits_dashboard, name="edits_dashboard"),
    path("shoots/<int:shoot_id>/", views.shoot_detail, name="shoot_detail"),
    path("shoots/<int:shoot_id>/manual-send/", views.manual_send, name="manual_send"),
    path("shoots/<int:shoot_id>/queue/add/", views.add_queue_invite, name="add_queue_invite"),
    path("shoots/<int:shoot_id>/reorder-invites/", views.reorder_invites, name="reorder_invites"),
    path("invites/<int:invite_id>/test-accept/", views.simulate_accept, name="simulate_accept"),
    path("invites/<int:invite_id>/test-escalate/", views.simulate_escalate, name="simulate_escalate"),
    path("webhook/pipedrive/", views.pipedrive_webhook, name="pipedrive_webhook"),
]
