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


@admin.register(Videographer)
class VideographerAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "city", "rating", "active", "email")
    list_filter = ("state", "active")
    search_fields = ("name", "email", "city")
    list_editable = ("active", "rating")


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
