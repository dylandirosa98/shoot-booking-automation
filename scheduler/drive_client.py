"""Google Drive folder creation and sharing for confirmed shoots."""
import logging
import uuid
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


@dataclass(frozen=True)
class DriveFolder:
    id: str
    url: str


class FakeDriveClient:
    """Logs Drive operations locally without creating or sharing real folders."""

    provider_name = "fake"

    def create_folder(self, *, name: str, shoot_id: int) -> DriveFolder:
        folder_id = f"fake-drive-{uuid.uuid4().hex[:12]}"
        url = f"https://drive.google.com/drive/folders/{folder_id}"
        logger.info("[FAKE DRIVE] CREATE folder %s for shoot %s: %s", folder_id, shoot_id, name)
        return DriveFolder(id=folder_id, url=url)

    def share_folder(self, *, folder_id: str, email: str) -> str:
        permission_id = f"fake-permission-{uuid.uuid4().hex[:12]}"
        logger.info("[FAKE DRIVE] SHARE folder %s with %s", folder_id, email)
        return permission_id

    def remove_permission(self, *, folder_id: str, permission_id: str) -> None:
        logger.info("[FAKE DRIVE] REMOVE permission %s from folder %s", permission_id, folder_id)


class GoogleDriveClient:
    """Real Drive API client using the existing long-lived OAuth refresh token."""

    provider_name = "google"

    def __init__(self):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def create_folder(self, *, name: str, shoot_id: int) -> DriveFolder:
        # The app property lets a retry find a folder that was created before a
        # transient database or network failure, rather than creating a duplicate.
        query = (
            f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false and "
            f"appProperties has {{ key='shoot_scheduler_shoot_id' and value='{shoot_id}' }}"
        )
        existing = self.service.files().list(
            q=query,
            spaces="drive",
            pageSize=1,
            fields="files(id, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])
        if existing:
            folder = existing[0]
            return DriveFolder(
                id=folder["id"],
                url=folder.get("webViewLink") or self._folder_url(folder["id"]),
            )

        body = {
            "name": name,
            "mimeType": FOLDER_MIME_TYPE,
            "appProperties": {"shoot_scheduler_shoot_id": str(shoot_id)},
        }
        if settings.GOOGLE_DRIVE_PARENT_FOLDER_ID:
            body["parents"] = [settings.GOOGLE_DRIVE_PARENT_FOLDER_ID]

        folder = self.service.files().create(
            body=body,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        return DriveFolder(
            id=folder["id"],
            url=folder.get("webViewLink") or self._folder_url(folder["id"]),
        )

    def share_folder(self, *, folder_id: str, email: str) -> str:
        # A retry might arrive after Drive accepted the share but before the
        # permission ID was saved locally. Reuse the existing direct permission.
        permissions = self.service.permissions().list(
            fileId=folder_id,
            fields="permissions(id, type, emailAddress)",
            supportsAllDrives=True,
        ).execute().get("permissions", [])
        for permission in permissions:
            if (permission.get("type") == "user" and
                    permission.get("emailAddress", "").lower() == email.lower()):
                return permission["id"]

        permission = self.service.permissions().create(
            fileId=folder_id,
            body={"type": "user", "role": "writer", "emailAddress": email},
            sendNotificationEmail=True,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        logger.info("[GOOGLE DRIVE] shared folder %s with %s", folder_id, email)
        return permission["id"]

    def remove_permission(self, *, folder_id: str, permission_id: str) -> None:
        self.service.permissions().delete(
            fileId=folder_id,
            permissionId=permission_id,
            supportsAllDrives=True,
        ).execute()
        logger.info("[GOOGLE DRIVE] removed permission %s from folder %s", permission_id, folder_id)

    @staticmethod
    def _folder_url(folder_id: str) -> str:
        return f"https://drive.google.com/drive/folders/{folder_id}"


def get_drive_client():
    """Use Drive when OAuth is configured; otherwise use the safe local fake."""
    if settings.GOOGLE_OAUTH_REFRESH_TOKEN:
        return GoogleDriveClient()
    return FakeDriveClient()

