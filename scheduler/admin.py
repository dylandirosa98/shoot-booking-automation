from django import forms
from django.contrib import admin
from .models import Videographer, Shoot, Invite, SchedulingSettings


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
