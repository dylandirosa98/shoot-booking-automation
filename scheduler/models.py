from django.db import models

NORTHEAST_STATE_CHOICES = [
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("MA", "Massachusetts"),
    ("ME", "Maine"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NY", "New York"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("VT", "Vermont"),
]


class SchedulingSettings(models.Model):
    """
    Singleton settings row, editable from the admin.
    Always use SchedulingSettings.get() to access — never query directly.
    """
    score_penalty_per_minute = models.FloatField(
        default=0.01,
        help_text="Rating points deducted per minute of estimated drive time. "
                  "0.01 = balanced. 0.02 = distance matters a lot. 0.005 = rating wins almost always.",
    )
    max_drive_minutes = models.IntegerField(
        default=180,
        help_text="Hard cap: skip any videographer whose drive time exceeds this.",
    )
    escalation_hours = models.FloatField(
        default=24.0,
        help_text="How long to wait for a videographer to accept before escalating to next ranked person.",
    )
    same_state_only = models.BooleanField(
        default=True,
        help_text="If checked, only consider videographers from the same state as the shoot.",
    )
    activity_type_filter = models.CharField(
        max_length=100,
        default="Shoot Booking",
        blank=True,
        help_text="Only Pipedrive activities whose type name CONTAINS this string will trigger a booking. "
                  "Leave blank to trigger on all activities.",
    )
    edit_activity_type_filter = models.CharField(
        max_length=100,
        default="Recruiting Highlight Video,Hype Video,Highlight Recap",
        blank=True,
        help_text="Comma-separated Pipedrive activity type names that trigger edit jobs.",
    )
    edit_subject_filter = models.CharField(
        max_length=100,
        default="",
        blank=True,
        help_text="Optional extra subject filter for edit jobs. Leave blank when using dedicated edit activity types.",
    )

    class Meta:
        verbose_name = "Scheduling Settings"
        verbose_name_plural = "Scheduling Settings"

    def __str__(self):
        return "Scheduling Settings"

    def save(self, *args, **kwargs):
        # enforce singleton: always pk=1
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Videographer(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    state = models.CharField(max_length=2, help_text="2-letter home/base state, e.g. MA")
    city = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True, help_text="Full address used for geocoding")
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    rating = models.FloatField(help_text="0.0 - 5.0")
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["state", "-rating"]

    @property
    def service_state_codes(self) -> list[str]:
        codes = [item.state for item in self.service_states.all()]
        return codes or ([self.state] if self.state else [])

    def __str__(self):
        return f"{self.name} ({self.state} - {self.rating} stars)"


class VideographerServiceState(models.Model):
    videographer = models.ForeignKey(
        Videographer,
        on_delete=models.CASCADE,
        related_name="service_states",
    )
    state = models.CharField(max_length=2, choices=NORTHEAST_STATE_CHOICES)

    class Meta:
        ordering = ["state", "videographer__name"]
        unique_together = [("videographer", "state")]

    def __str__(self):
        return f"{self.videographer.name} serves {self.state}"


VIDEO_TYPE_CHOICES = [
    ("Highlight", "Highlight"),
    ("Hype", "Hype"),
    ("Recruiting", "Recruiting"),
]


class Editor(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    clickup_user_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="ClickUp user ID used when assigning edit tasks.",
    )
    max_active_jobs = models.PositiveIntegerField(default=5)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class EditorVideoTypeRank(models.Model):
    editor = models.ForeignKey(Editor, on_delete=models.CASCADE, related_name="video_type_ranks")
    video_type = models.CharField(max_length=30, choices=VIDEO_TYPE_CHOICES)
    rank = models.PositiveIntegerField(help_text="1 is the first choice for this video type.")
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["video_type", "rank", "editor__name"]
        unique_together = [("editor", "video_type"), ("video_type", "rank")]

    def __str__(self):
        return f"{self.video_type}: #{self.rank} {self.editor.name}"


class Shoot(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending - looking for videographer"),
        ("confirmed", "Confirmed"),
        ("failed", "Failed - no one accepted"),
        ("cancelled", "Cancelled"),
    ]

    pipedrive_deal_id = models.CharField(max_length=50, db_index=True, null=True, blank=True,
        help_text="Pipedrive deal id (a deal can have multiple shoot activities — NOT unique)")
    pipedrive_activity_id = models.CharField(max_length=50, unique=True, null=True, blank=True,
        help_text="Pipedrive activity id — the actual unique identifier for one shoot")
    title = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=255)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    shoot_datetime = models.DateTimeField()
    duration_minutes = models.IntegerField(default=120)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    confirmed_videographer = models.ForeignKey(
        Videographer, null=True, blank=True, on_delete=models.SET_NULL, related_name="confirmed_shoots"
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-shoot_datetime"]

    def __str__(self):
        return f"{self.title or 'Shoot'} @ {self.location} ({self.shoot_datetime:%Y-%m-%d %H:%M})"


class EditJob(models.Model):
    STATUS_CHOICES = [
        ("created", "Created"),
        ("updated", "Updated"),
        ("failed", "Failed - no editor available"),
        ("clickup_failed", "Failed - ClickUp task not created"),
        ("cancelled", "Cancelled"),
    ]
    ACTIVE_STATUSES = {"created", "updated"}

    pipedrive_deal_id = models.CharField(max_length=50, db_index=True, null=True, blank=True)
    pipedrive_activity_id = models.CharField(
        max_length=50,
        unique=True,
        null=True,
        blank=True,
        help_text="Pipedrive activity id - the unique identifier for one edit job",
    )
    title = models.CharField(max_length=255, blank=True)
    video_type = models.CharField(max_length=30, choices=VIDEO_TYPE_CHOICES, blank=True)
    due_datetime = models.DateTimeField()
    duration_minutes = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="created")
    assigned_editor = models.ForeignKey(
        Editor, null=True, blank=True, on_delete=models.SET_NULL, related_name="edit_jobs"
    )
    clickup_task_id = models.CharField(max_length=80, blank=True)
    clickup_error = models.TextField(blank=True)
    clickup_synced_at = models.DateTimeField(null=True, blank=True)
    active_job_count_at_assignment = models.PositiveIntegerField(default=0)
    selection_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_datetime"]

    def __str__(self):
        return f"{self.title or 'Edit'} ({self.due_datetime:%Y-%m-%d %H:%M})"


class Invite(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("declined", "Declined"),
        ("expired", "Expired (no response in 24h)"),
        ("cancelled", "Cancelled (someone else accepted)"),
    ]

    shoot = models.ForeignKey(Shoot, on_delete=models.CASCADE, related_name="invites")
    videographer = models.ForeignKey(Videographer, on_delete=models.CASCADE, related_name="invites")
    rank = models.IntegerField(help_text="Order in the ranked list (0 = top pick)")
    score = models.FloatField(help_text="Score at time of ranking (rating - penalty*drive_min)")
    drive_minutes = models.FloatField(null=True, blank=True)
    drive_miles = models.FloatField(null=True, blank=True)
    google_event_id = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    sent_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["shoot", "rank"]
        unique_together = [("shoot", "videographer")]

    def __str__(self):
        return f"{self.videographer.name} -> {self.shoot} (#{self.rank}, {self.status})"
