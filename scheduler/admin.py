from django import forms
from django.contrib import admin
from .models import Editor, EditorVideoTypeRank, EditJob, Videographer, Shoot, Invite, SchedulingSettings


@admin.register(SchedulingSettings)
class SchedulingSettingsAdmin(admin.ModelAdmin):
    """Singleton admin — disable add/delete, force editing the one row."""

    def has_add_permission(self, request):
        return not SchedulingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = SchedulingSettings.get()
        from django.shortcuts import redirect
        return redirect(f"./{obj.pk}/change/")


class VideographerAdminForm(forms.ModelForm):
    class Meta:
        model = Videographer
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lat"].required = True
        self.fields["lng"].required = True
        self.fields["lat"].help_text = (
            "Required. Get coords from "
            "<a href='https://www.latlong.net/' target='_blank'>latlong.net</a> "
            "or right-click a location in Google Maps."
        )
        self.fields["lng"].help_text = "Required."


@admin.register(Videographer)
class VideographerAdmin(admin.ModelAdmin):
    form = VideographerAdminForm
    list_display = ("name", "state", "city", "rating", "active", "has_coords", "email")
    list_filter = ("state", "active")
    search_fields = ("name", "email", "city")
    list_editable = ("active", "rating")

    @admin.display(boolean=True, description="Coords?")
    def has_coords(self, obj):
        return obj.lat is not None and obj.lng is not None


class InviteInline(admin.TabularInline):
    model = Invite
    extra = 0
    readonly_fields = ("sent_at", "responded_at", "score", "drive_minutes", "drive_miles")


class EditorVideoTypeRankInline(admin.TabularInline):
    model = EditorVideoTypeRank
    extra = 0


@admin.register(Editor)
class EditorAdmin(admin.ModelAdmin):
    list_display = ("name", "max_active_jobs", "active", "clickup_user_id", "email")
    list_filter = ("active",)
    search_fields = ("name", "email", "clickup_user_id")
    list_editable = ("active", "max_active_jobs")
    inlines = [EditorVideoTypeRankInline]


@admin.register(EditorVideoTypeRank)
class EditorVideoTypeRankAdmin(admin.ModelAdmin):
    list_display = ("video_type", "rank", "editor", "active")
    list_filter = ("video_type", "active")
    search_fields = ("editor__name", "editor__email")
    list_editable = ("rank", "active")


@admin.register(EditJob)
class EditJobAdmin(admin.ModelAdmin):
    list_display = ("title", "video_type", "due_datetime", "status", "assigned_editor", "clickup_task_id", "active_job_count_at_assignment")
    list_filter = ("status", "assigned_editor")
    search_fields = ("title", "pipedrive_deal_id", "pipedrive_activity_id", "assigned_editor__name")
    readonly_fields = ("created_at", "updated_at", "active_job_count_at_assignment", "clickup_synced_at", "clickup_error")


@admin.register(Shoot)
class ShootAdmin(admin.ModelAdmin):
    list_display = ("title", "location", "shoot_datetime", "status", "confirmed_videographer")
    list_filter = ("status",)
    search_fields = ("title", "location", "pipedrive_deal_id")
    inlines = [InviteInline]


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    list_display = ("videographer", "shoot", "rank", "status", "sent_at", "expires_at")
    list_filter = ("status",)
    search_fields = ("videographer__name", "shoot__title")
